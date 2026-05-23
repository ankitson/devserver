"""Top-level Dagster Definitions object."""

from __future__ import annotations

from datetime import date, timedelta

from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    EnvVar,
    RunRequest,
    ScheduleDefinition,
    define_asset_job,
    in_process_executor,
    schedule,
)

from pipeline_dagster_proj.assets import (
    anomaly_candidates,
    derived_daily,
    raw_assets,
    raw_heart_rate_samples,
    validate_heart_rate,
    validate_hrv,
    validate_sleep,
)
from pipeline_dagster_proj.banking import (
    bank_login_job,
    bank_otp_sensor,
    bank_resume_job,
    notifier_sensor,
)
from pipeline_dagster_proj.filecopy import make_copy_pipeline
from pipeline_dagster_proj.gameactivity import (
    playnite_gameactivity_job,
    playnite_landing_sensor,
)
from pipeline_dagster_proj.housekeeping import (
    landing_zone_gc_job,
    landing_zone_gc_schedule,
)
from pipeline_dagster_proj.resources import GarminPipelineResource


# File-copy pipeline: drop a batch into landing_zone/aoe4-replay-fetcher/,
# its contents get copied into /aoe4-replays (a bind mount). Archives kept 2
# days only (see housekeeping.ARCHIVE_RETENTION_OVERRIDES).
aoe4_replays_copy_job, aoe4_replays_landing_sensor = make_copy_pipeline(
    pipeline_name="aoe4-replay-fetcher",
    dest_dir="/aoe4-replays",
)


all_assets = [
    *raw_assets,
    raw_heart_rate_samples,
    derived_daily,
    anomaly_candidates,
]

asset_checks = [validate_sleep, validate_heart_rate, validate_hrv]

garmin_full_job = define_asset_job(
    "garmin_full_job",
    selection=AssetSelection.all(),
    # Single-process executor so all ops in one run share the in-process Garmin
    # rate limiter. Multiprocess would put each op in its own subprocess and
    # the rate-limit gate wouldn't be coordinated.
    executor_def=in_process_executor,
)


# 3x daily — same cadence as the deprecated garmin-fetch cron. The in-process
# rate limiter (2.5s floor) plus single-process executor keeps the live API
# calls well under any Garmin limit.
# default_status STOPPED — turn on explicitly via the UI or
# `dagster schedule start garmin_daily_schedule` once cache is fully backfilled
# and any rate-limit cooldown has cleared.
@schedule(
    cron_schedule="0 8,14,22 * * *",
    job=garmin_full_job,
    default_status=DefaultScheduleStatus.STOPPED,
)
def garmin_daily_schedule(context):
    # Anchor to the scheduled fire time (UTC by default), not date.today() —
    # date.today() drifts with the daemon's wall clock, and a late evaluation
    # can roll over to a partition the partitions_def hasn't included yet.
    fire_date = context.scheduled_execution_time.date()
    today = fire_date.isoformat()
    yesterday = (fire_date - timedelta(days=1)).isoformat()
    return [
        RunRequest(partition_key=today, run_key=f"daily-{today}"),
        RunRequest(partition_key=yesterday, run_key=f"daily-{yesterday}-{context.scheduled_execution_time.isoformat()}"),
    ]


defs = Definitions(
    assets=all_assets,
    asset_checks=asset_checks,
    jobs=[garmin_full_job, bank_login_job, bank_resume_job,
          playnite_gameactivity_job, landing_zone_gc_job,
          aoe4_replays_copy_job],
    schedules=[garmin_daily_schedule, landing_zone_gc_schedule],
    sensors=[bank_otp_sensor, notifier_sensor, playnite_landing_sensor,
             aoe4_replays_landing_sensor],
    resources={
        "garmin": GarminPipelineResource(tool="dagster"),
    },
    # Global default — applies to asset backfills launched from Dagit too,
    # which is what we want: every raw_* op runs in the same process as the
    # pipeline-shared rate limiter (no parallel logins).
    executor=in_process_executor,
)
