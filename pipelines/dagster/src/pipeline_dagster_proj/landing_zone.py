"""Generic landing_zone scanner — reused by each per-pipeline sensor.

Filesystem contract (see landing_zone/README.md):

  $LANDING_ZONE_DIR / <pipeline> / <batch_id> / *.json + _READY

A batch is "ready" when its subdir contains a `_READY` marker file.

`.tmp` files are ignored — never included in the file list passed to the job
but their presence does NOT delay processing. The client guarantees, via the
contract, that any file it still wants the pipeline to see has been renamed
out of `.tmp` BEFORE the `_READY` marker is written.

After a job processes a batch successfully it calls `archive_batch(...)` which
moves the entire batch subdir to `_archive/<pipeline>/YYYY-MM-DD/<batch_id>/`.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

log = logging.getLogger(__name__)

ARCHIVE_DIRNAME = "_archive"
FAILED_DIRNAME = "_failed"


READY_MARKER = "_READY"
TMP_SUFFIX = ".tmp"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
WAKE_BLACKOUT_START_HOUR = 8
WAKE_BLACKOUT_MINUTES = 5


def landing_zone_root() -> Path:
    """Root from env. Falls back to the host-side path so unit tests work
    without docker."""
    raw = os.environ.get("LANDING_ZONE_DIR", "/landing_zone")
    return Path(raw)


def is_pc_wake_blackout(now: datetime | None = None) -> bool:
    """Skip landing-zone scans while the PC and Synology mount settle."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(PACIFIC_TZ)
    return (
        local_now.hour == WAKE_BLACKOUT_START_HOUR
        and 0 <= local_now.minute < WAKE_BLACKOUT_MINUTES
    )


def mark_batch(database_url: str, pipeline: str, batch_id: str, **fields) -> None:
    """Upsert a landing_zone_batches row. Merges the given fields via ON
    CONFLICT, so it can be called repeatedly across a batch's lifecycle
    (ready -> running -> done/failed). Pass error=None to clear a prior error."""
    cols = ["pipeline", "batch_id", *fields.keys()]
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in fields.keys())
    sql = (
        f"INSERT INTO landing_zone_batches ({', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (pipeline, batch_id) DO UPDATE SET {updates}"
    )
    values = [pipeline, batch_id, *fields.values()]
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)


@dataclass(frozen=True)
class ReadyBatch:
    pipeline: str
    batch_id: str
    batch_dir: Path
    files: list[Path]  # data files only — excludes _READY and dotfiles
    ready_mtime_ns: int  # for stable run_key — re-ready-ing the same name fires again


def scan_ready_batches(pipeline: str) -> list[ReadyBatch]:
    """List ready batches under landing_zone_root/<pipeline>/.

    Returns [] if the pipeline dir is missing — sensor treats that as "nothing
    to do", not an error.
    """
    base = landing_zone_root() / pipeline
    if not base.is_dir():
        return []
    out: list[ReadyBatch] = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        marker = sub / READY_MARKER
        if not marker.exists():
            continue
        entries = list(sub.iterdir())
        # .tmp files are silently ignored — their presence does not block the
        # batch from being considered ready. The client must ensure any file
        # it wants picked up has been renamed off `.tmp` before writing _READY.
        data_files = sorted(
            p for p in entries
            if p.is_file()
            and not p.name.startswith("_")
            and not p.name.startswith(".")
            and not p.name.endswith(TMP_SUFFIX)
        )
        out.append(ReadyBatch(
            pipeline=pipeline,
            batch_id=sub.name,
            batch_dir=sub,
            files=data_files,
            ready_mtime_ns=marker.stat().st_mtime_ns,
        ))
    return out


def archive_batch(*, pipeline: str, batch_dir: Path, on_date: date | None = None) -> Path:
    """Move batch_dir to _archive/<pipeline>/<YYYY-MM-DD>/<batch_id>/.

    Returns the new path. If the destination already exists (re-running an
    archived batch_id), suffix with `-rerun-N`.
    """
    when = (on_date or date.today()).isoformat()
    archive_root = landing_zone_root() / ARCHIVE_DIRNAME / pipeline / when
    archive_root.mkdir(parents=True, exist_ok=True)
    dest = archive_root / batch_dir.name
    n = 1
    while dest.exists():
        dest = archive_root / f"{batch_dir.name}-rerun-{n}"
        n += 1
    shutil.move(str(batch_dir), str(dest))
    return dest


# --- garbage collection -------------------------------------------------
#
# Retention policy (see the daily landing_zone_gc schedule):
#   - successful archives under _archive/<pipeline>/<YYYY-MM-DD>/ are deleted
#     once the date-dir is older than `retention_days` (default 30).
#   - failed batches sit in the inbox for retry; after `age_days` (default 14)
#     they're swept into _archive/_failed/<pipeline>/<YYYY-MM-DD>/<batch_id>/
#     (moved, never deleted) so the inbox stays clean but failures stay
#     inspectable. The ledger row keeps the original error.
# Ledger rows themselves are never deleted — they're the permanent audit trail.


def prune_archived(*, retention_days: int = 30,
                   overrides: dict[str, int] | None = None,
                   today: date | None = None) -> dict:
    """Delete _archive/<pipeline>/<YYYY-MM-DD>/ dirs older than the pipeline's
    retention window (`overrides[pipeline]` if set, else `retention_days`).

    Skips the _failed quarantine dir and any non-date-named dir. Returns
    {'deleted': [paths], 'batches': N}.
    """
    overrides = overrides or {}
    archive_root = landing_zone_root() / ARCHIVE_DIRNAME
    base = today or date.today()
    deleted: list[str] = []
    batches = 0
    if not archive_root.is_dir():
        return {"deleted": deleted, "batches": batches}
    for pipeline_dir in archive_root.iterdir():
        if not pipeline_dir.is_dir() or pipeline_dir.name == FAILED_DIRNAME:
            continue
        cutoff = base - timedelta(days=overrides.get(pipeline_dir.name, retention_days))
        for date_dir in pipeline_dir.iterdir():
            if not date_dir.is_dir():
                continue
            try:
                d = date.fromisoformat(date_dir.name)
            except ValueError:
                continue  # not a YYYY-MM-DD dir — leave it alone
            if d < cutoff:
                batches += sum(1 for _ in date_dir.iterdir())
                shutil.rmtree(date_dir)
                deleted.append(str(date_dir))
                log.info("GC pruned archived date-dir %s", date_dir)
    return {"deleted": deleted, "batches": batches}


def sweep_failed(*, database_url: str, age_days: int = 14,
                 now: datetime | None = None) -> dict:
    """Move inbox dirs of failed batches older than age_days into
    _archive/_failed/<pipeline>/<YYYY-MM-DD>/<batch_id>/.

    A batch is eligible when its ledger row is status='failed', not yet swept
    (archived_to IS NULL), and its failure (finished_at, else ready_at) is older
    than age_days. The dir is moved (not deleted) and the ledger row updated
    with archived_to. Returns {'moved': [batch_ids]}.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=age_days)
    root = landing_zone_root()
    moved: list[str] = []
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pipeline, batch_id, COALESCE(finished_at, ready_at)
                  FROM landing_zone_batches
                 WHERE status = 'failed'
                   AND archived_to IS NULL
                   AND COALESCE(finished_at, ready_at) < %s
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
        for pipeline, batch_id, failed_at in rows:
            src = root / pipeline / batch_id
            if not src.is_dir():
                continue  # already gone from the inbox; skip
            when = (failed_at.date() if failed_at else now.date()).isoformat()
            dest_parent = root / ARCHIVE_DIRNAME / FAILED_DIRNAME / pipeline / when
            dest_parent.mkdir(parents=True, exist_ok=True)
            dest = dest_parent / batch_id
            n = 1
            while dest.exists():
                dest = dest_parent / f"{batch_id}-{n}"
                n += 1
            shutil.move(str(src), str(dest))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE landing_zone_batches SET archived_to = %s
                     WHERE pipeline = %s AND batch_id = %s
                    """,
                    (str(dest), pipeline, batch_id),
                )
            conn.commit()
            moved.append(batch_id)
            log.info("GC swept failed batch %s/%s -> %s", pipeline, batch_id, dest)
    return {"moved": moved}
