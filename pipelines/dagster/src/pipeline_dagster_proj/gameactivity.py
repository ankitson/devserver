"""Playnite GameActivity pipeline.

One job (`playnite_gameactivity_job`) processes one batch dropped into
`landing_zone/playnite-gameactivity/<batch_id>/`. A sensor
(`playnite_landing_sensor`) polls the directory every 30 s and emits one
RunRequest per ready batch.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg
from dagster import (
    DefaultSensorStatus,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    SkipReason,
    job,
    op,
    sensor,
)

from pipeline_shared import gameactivity_import_batch
from pipeline_shared.config import load_settings
from pipeline_shared.schema import ensure_schema

from pipeline_dagster_proj.landing_zone import (
    archive_batch,
    is_pc_wake_blackout,
    scan_ready_batches,
)

log = logging.getLogger(__name__)

PIPELINE_NAME = "playnite-gameactivity"


def _mark(database_url: str, batch_id: str, **fields) -> None:
    """Upsert a row in landing_zone_batches. Called at multiple points in the
    job lifecycle; uses ON CONFLICT to merge updates."""
    cols = ["pipeline", "batch_id", *fields.keys()]
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in fields.keys())
    sql = (
        f"INSERT INTO landing_zone_batches ({', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (pipeline, batch_id) DO UPDATE SET {updates}"
    )
    values = [PIPELINE_NAME, batch_id, *fields.values()]
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)


@op(config_schema={"batch_id": str, "batch_dir": str})
def process_playnite_batch(context) -> dict:
    """Parse every JSON in batch_dir, upsert sessions, archive the dir."""
    batch_id = context.op_config["batch_id"]
    batch_dir = Path(context.op_config["batch_dir"])
    s = load_settings()
    ensure_schema(s.database_url)

    if not batch_dir.is_dir():
        raise RuntimeError(f"batch dir disappeared between sensor and run: {batch_dir}")

    file_names = [p.name for p in sorted(batch_dir.iterdir()) if p.is_file()]
    _mark(
        s.database_url, batch_id,
        status="running",
        files=json.dumps(file_names),
        file_count=len([n for n in file_names if n.endswith(".json")]),
    )
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE landing_zone_batches SET started_at = NOW() "
                "WHERE pipeline = %s AND batch_id = %s",
                (PIPELINE_NAME, batch_id),
            )

    try:
        counts = gameactivity_import_batch(
            database_url=s.database_url,
            batch_dir=batch_dir,
            batch_id=batch_id,
        )
    except Exception as e:
        _mark(s.database_url, batch_id, status="failed", error=str(e))
        with psycopg.connect(s.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE landing_zone_batches SET finished_at = NOW() "
                    "WHERE pipeline = %s AND batch_id = %s",
                    (PIPELINE_NAME, batch_id),
                )
        raise

    dest = archive_batch(pipeline=PIPELINE_NAME, batch_dir=batch_dir)
    _mark(
        s.database_url, batch_id,
        status="done",
        archived_to=str(dest),
        row_count=counts["rows"],
        error=None,  # clear any error left from a prior failed attempt
    )
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE landing_zone_batches SET finished_at = NOW() "
                "WHERE pipeline = %s AND batch_id = %s",
                (PIPELINE_NAME, batch_id),
            )
    context.log.info(
        "playnite batch %s: %d files, %d rows, %d games -> %s",
        batch_id, counts["files"], counts["rows"], counts["games"], dest,
    )
    return {"batch_id": batch_id, **counts, "archived_to": str(dest)}


@job
def playnite_gameactivity_job():
    process_playnite_batch()


@sensor(
    job=playnite_gameactivity_job,
    minimum_interval_seconds=30,
    default_status=DefaultSensorStatus.RUNNING,
)
def playnite_landing_sensor(context: SensorEvaluationContext):
    """Poll landing_zone/playnite-gameactivity/ for ready batches."""
    if is_pc_wake_blackout():
        return SkipReason("PC wake blackout: skipping 08:00-08:05 America/Los_Angeles")

    batches = scan_ready_batches(PIPELINE_NAME)
    if not batches:
        return SkipReason("no ready batches")

    s = load_settings()
    ensure_schema(s.database_url)

    requests = []
    for b in batches:
        # Register the batch in the ledger as soon as we observe it ready. Safe
        # to call repeatedly — ON CONFLICT keeps the earlier ready_at.
        _mark(s.database_url, b.batch_id, status="ready",
              file_count=len(b.files),
              files=json.dumps([p.name for p in b.files]))
        # run_key includes the marker mtime so re-creating a batch with the
        # same id (after the previous one was archived) re-fires.
        run_key = f"{b.pipeline}:{b.batch_id}:{b.ready_mtime_ns}"
        requests.append(RunRequest(
            run_key=run_key,
            run_config={
                "ops": {
                    "process_playnite_batch": {
                        "config": {
                            "batch_id": b.batch_id,
                            "batch_dir": str(b.batch_dir),
                        }
                    }
                }
            },
        ))
    return SensorResult(requests)
