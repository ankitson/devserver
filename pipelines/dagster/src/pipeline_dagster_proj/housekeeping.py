"""Landing-zone housekeeping — daily GC of archived + failed batches.

Generic across all file-drop pipelines (not Playnite-specific). Policy:
  - delete successful archives older than ARCHIVE_RETENTION_DAYS
  - sweep failed batches still in the inbox into _archive/_failed after
    FAILED_SWEEP_DAYS (moved, never deleted)
Ledger rows in landing_zone_batches are never deleted.
"""

from __future__ import annotations

from dagster import (
    DefaultScheduleStatus,
    RunRequest,
    job,
    op,
    schedule,
)

from pipeline_shared.config import load_settings
from pipeline_shared.schema import ensure_schema

from pipeline_dagster_proj.landing_zone import prune_archived, sweep_failed

ARCHIVE_RETENTION_DAYS = 30
FAILED_SWEEP_DAYS = 14

# Per-pipeline archive retention overrides (days). Pipelines not listed use
# ARCHIVE_RETENTION_DAYS. aoe4-replay-fetcher just copies files out to a live
# mount, so its archive is a short-lived safety net.
ARCHIVE_RETENTION_OVERRIDES = {
    "aoe4-replay-fetcher": 2,
}


@op
def gc_landing_zone(context) -> dict:
    s = load_settings()
    ensure_schema(s.database_url)
    pruned = prune_archived(
        retention_days=ARCHIVE_RETENTION_DAYS,
        overrides=ARCHIVE_RETENTION_OVERRIDES,
    )
    swept = sweep_failed(database_url=s.database_url, age_days=FAILED_SWEEP_DAYS)
    context.log.info(
        "landing_zone GC: pruned %d archive date-dir(s) (%d batches), "
        "swept %d failed batch(es) to _failed",
        len(pruned["deleted"]), pruned["batches"], len(swept["moved"]),
    )
    return {"pruned": pruned, "swept": swept}


@job
def landing_zone_gc_job():
    gc_landing_zone()


# Daily at 09:30 UTC — offset from the Garmin schedule (08/14/22) so it doesn't
# pile onto a fetch window. RUNNING by default: nothing destructive fires until
# archives are >30d / failures are >14d old, so it's safe to leave on.
@schedule(
    cron_schedule="30 9 * * *",
    job=landing_zone_gc_job,
    default_status=DefaultScheduleStatus.RUNNING,
)
def landing_zone_gc_schedule(context):
    return RunRequest(run_key=f"lz-gc-{context.scheduled_execution_time.date()}")
