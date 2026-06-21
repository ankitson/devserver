"""Reusable file-copy landing-zone pipeline.

Some drops don't need parsing — they just need their files copied somewhere.
`make_copy_pipeline(pipeline_name, dest_dir)` returns a (job, sensor) pair that:

  1. watches landing_zone/<pipeline_name>/ for ready batches (same contract as
     every other landing-zone pipeline: _READY marker, .tmp ignored),
  2. copies the whole batch directory's contents into `dest_dir` (overlay:
     same-named files are overwritten, others left in place),
  3. archives the batch under _archive/<pipeline_name>/<date>/ and records it in
     the landing_zone_batches ledger.

No DB rows beyond the ledger. Register the returned job + sensor in
definitions.py. Archive retention is controlled per-pipeline by the
landing_zone GC (see housekeeping.ARCHIVE_RETENTION_OVERRIDES).
"""

from __future__ import annotations

import shutil
from pathlib import Path

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

from pipeline_shared.config import load_settings
from pipeline_shared.schema import ensure_schema

from pipeline_dagster_proj.landing_zone import (
    READY_MARKER,
    TMP_SUFFIX,
    archive_batch,
    is_pc_wake_blackout,
    mark_batch,
    scan_ready_batches,
)


def _slug(pipeline_name: str) -> str:
    return pipeline_name.replace("-", "_")


def _copy_overlay(batch_dir: Path, dest: Path) -> int:
    """Copy batch_dir's contents into dest (merging into existing dirs),
    skipping the _READY marker and any .tmp files. Returns files copied."""
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for child in batch_dir.iterdir():
        if child.name == READY_MARKER or child.name.endswith(TMP_SUFFIX):
            continue
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(
                child, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("*" + TMP_SUFFIX),
            )
            copied += sum(1 for p in child.rglob("*") if p.is_file())
        else:
            shutil.copy2(child, target)
            copied += 1
    return copied


def make_copy_pipeline(*, pipeline_name: str, dest_dir: str):
    """Build a (job, sensor) that copies each ready batch into dest_dir."""
    slug = _slug(pipeline_name)
    op_name = f"{slug}_copy_batch"
    job_name = f"{slug}_copy_job"
    sensor_name = f"{slug}_landing_sensor"

    @op(name=op_name, config_schema={"batch_id": str, "batch_dir": str})
    def copy_batch(context) -> dict:
        batch_id = context.op_config["batch_id"]
        batch_dir = Path(context.op_config["batch_dir"])
        s = load_settings()
        ensure_schema(s.database_url)
        if not batch_dir.is_dir():
            raise RuntimeError(f"batch dir gone between sensor and run: {batch_dir}")

        mark_batch(s.database_url, pipeline_name, batch_id, status="running")
        try:
            copied = _copy_overlay(batch_dir, Path(dest_dir))
        except Exception as e:
            mark_batch(s.database_url, pipeline_name, batch_id,
                       status="failed", error=str(e))
            raise

        dest = archive_batch(pipeline=pipeline_name, batch_dir=batch_dir)
        mark_batch(s.database_url, pipeline_name, batch_id,
                   status="done", archived_to=str(dest),
                   row_count=copied, error=None)
        context.log.info("%s batch %s: copied %d file(s) -> %s (archived %s)",
                         pipeline_name, batch_id, copied, dest_dir, dest)
        return {"batch_id": batch_id, "copied": copied,
                "dest": dest_dir, "archived_to": str(dest)}

    @job(name=job_name)
    def copy_job():
        copy_batch()

    @sensor(name=sensor_name, job=copy_job, minimum_interval_seconds=30,
            default_status=DefaultSensorStatus.RUNNING)
    def copy_sensor(context: SensorEvaluationContext):
        if is_pc_wake_blackout():
            return SkipReason(
                "PC wake blackout: skipping 08:00-08:05 America/Los_Angeles"
            )

        batches = scan_ready_batches(pipeline_name)
        if not batches:
            return SkipReason("no ready batches")
        s = load_settings()
        ensure_schema(s.database_url)
        requests = []
        # Submit oldest-ready first so that, under the landing_copy serialization
        # limit, the newest batch overlays last and wins on same-named files.
        for b in sorted(batches, key=lambda b: b.ready_mtime_ns):
            mark_batch(s.database_url, pipeline_name, b.batch_id, status="ready")
            run_key = f"{b.pipeline}:{b.batch_id}:{b.ready_mtime_ns}"
            requests.append(RunRequest(
                run_key=run_key,
                # Serialize copies of this pipeline (see dagster.yaml
                # landing_copy limit) so concurrent batches don't race on
                # same-named files in dest_dir.
                tags={"landing_copy": pipeline_name},
                run_config={"ops": {op_name: {"config": {
                    "batch_id": b.batch_id,
                    "batch_dir": str(b.batch_dir),
                }}}},
            ))
        return SensorResult(requests)

    return copy_job, copy_sensor
