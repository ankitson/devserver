"""Top-level Dagster Definitions object."""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone

from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    EnvVar,
    In,
    RunRequest,
    ScheduleDefinition,
    define_asset_job,
    in_process_executor,
    job,
    op,
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
from pipeline_dagster_proj.bookmarks_import import (
    birdclaw_bookmarks_import_job,
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


# Gap-healing — daily force-refetch of recent days still missing sleep data.
# The daily schedule above only force-refetches today + yesterday; a watch that
# syncs late (Garmin serves an all-null dailySleepDTO until the device uploads,
# sometimes several days later) leaves a permanent hole in the deeper tail that
# nothing revisits. This walks the last HEAL_WINDOW_DAYS and refetches ONLY the
# days with no sleep score — a handful of SELECTs in steady state, real API work
# only on an actual gap. Tagged garmin_api so the QueuedRunCoordinator serialises
# it against garmin_full_job (no parallel logins / rate-limit collisions).
HEAL_WINDOW_DAYS = 10


@op(tags={"dagster/concurrency_key": "garmin_api"})
def garmin_heal_op(context, garmin: GarminPipelineResource) -> None:
    from pipeline_shared import heal_missing_days
    run = garmin.make_run(context.run_id)
    result = heal_missing_days(
        run, garmin.settings().database_url, window_days=HEAL_WINDOW_DAYS,
    )
    context.log.info(
        "garmin heal: window=%s..%s missing=%s healed=%s",
        result["window"][-1], result["window"][0],
        result["missing"] or "none", result["healed"] or "none",
    )


@job(tags={"dagster/concurrency_key": "garmin_api"})
def garmin_heal_job():
    garmin_heal_op()


# Daily at 06:00 UTC — before the 08:00 daily fetch. default_status STOPPED to
# match garmin_daily_schedule; start it from the UI alongside the daily one.
@schedule(
    cron_schedule="0 6 * * *",
    job=garmin_heal_job,
    default_status=DefaultScheduleStatus.STOPPED,
)
def garmin_heal_schedule(context):
    return RunRequest(
        run_key=f"garmin-heal-{context.scheduled_execution_time:%Y%m%d}"
    )


# birdclaw sync — Dagster as a plain scheduler for `docker exec` into the
# app-runner container (which owns birdclaw + its SQLite). Needs the Docker
# socket mounted into pipeline-dagster + a docker CLI in the image. Hourly,
# mirroring the retired DBOS x_bookmarks tick; idempotent + cheap (birdclaw
# early-stops), so RUNNING by default.
_BIRDCLAW_EXEC = [
    "docker", "exec",
    "-e", "BIRDCLAW_HOME=/data/birdclaw",
    # HOME points xurl at the directory-mounted token (/data/birdclaw/.xurl);
    # the old single-file .xurl bind mount went stale on token refresh.
    "-e", "HOME=/data/birdclaw",
    "-e", "BIRDCLAW_BIRD_COMMAND=/home/ankit/.local/bin/bird",
    "-w", "/apps/birdclaw",
    "app-runner",
    "node", "bin/birdclaw.mjs", "--json",
]


def _run_birdclaw(context, args: list[str]) -> str:
    cmd = _BIRDCLAW_EXEC + args
    context.log.info("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.stdout.strip():
        context.log.info("stdout:\n%s", proc.stdout.strip()[-4000:])
    if proc.stderr.strip():
        context.log.info("stderr:\n%s", proc.stderr.strip()[-4000:])
    if proc.returncode != 0:
        raise Exception(f"birdclaw exited {proc.returncode}")
    return proc.stdout


def _account_sync_op(account: str):
    # One op per account (the account is closed over, not a graph input).
    # `after` is an optional ordering input: when wired, it forces sequential
    # execution so the two syncs don't contend on the shared birdclaw store /
    # X rate limit.
    @op(name=f"birdclaw_sync_{account}", ins={"after": In(str, default_value="")})
    def _op(context, after) -> str:
        thread_since = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        return _run_birdclaw(context, [
            "jobs", "sync-account", "--account", account,
            "--mode", "auto", "--steps", "likes,bookmarks", "--max-pages", "50",
            "--refresh", "--saved-author-threads",
            "--saved-author-threads-since", thread_since,
            "--saved-author-threads-delay-ms", "5000",
            "--saved-author-thread-page-delay-ms", "2000",
        ])
    return _op


birdclaw_sync_primary_op = _account_sync_op("acct_primary")
birdclaw_sync_abiosno_op = _account_sync_op("acct_abiosno")


@job
def birdclaw_sync_bookmarks_job():
    birdclaw_sync_primary_op()


@schedule(
    cron_schedule="0 * * * *",
    job=birdclaw_sync_bookmarks_job,
    default_status=DefaultScheduleStatus.RUNNING,
)
def birdclaw_sync_bookmarks_schedule(context):
    return RunRequest(
        run_key=f"birdclaw-bookmarks-{context.scheduled_execution_time:%Y%m%d%H}"
    )


# Mirrors birdclaw's SQLite bookmarks into postgres. Runs 15 min after the
# sync job so the freshly-pulled bookmarks are picked up the same hour.
# Idempotent upsert, so retries / overlaps are safe.
@schedule(
    cron_schedule="15 * * * *",
    job=birdclaw_bookmarks_import_job,
    default_status=DefaultScheduleStatus.RUNNING,
)
def birdclaw_bookmarks_import_schedule(context):
    return RunRequest(
        run_key=f"birdclaw-bookmarks-import-{context.scheduled_execution_time:%Y%m%d%H}"
    )


defs = Definitions(
    assets=all_assets,
    asset_checks=asset_checks,
    jobs=[garmin_full_job, garmin_heal_job, bank_login_job, bank_resume_job,
          playnite_gameactivity_job, landing_zone_gc_job,
          aoe4_replays_copy_job, birdclaw_sync_bookmarks_job,
          birdclaw_bookmarks_import_job],
    schedules=[garmin_daily_schedule, garmin_heal_schedule,
               landing_zone_gc_schedule,
               birdclaw_sync_bookmarks_schedule,
               birdclaw_bookmarks_import_schedule],
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
