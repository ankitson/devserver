"""Daily-partitioned Garmin assets.

Per the contract, each metric is a separate asset so a per-partition failure
isolates to that one metric+date. The Garmin API is a shared resource —
we annotate all raw-tier assets with `op_tags={"dagster/concurrency_key":
"garmin_api"}` so backfills can't fan out and trip rate limits.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from dagster import (
    AssetCheckResult,
    AssetExecutionContext,
    Backoff,
    DailyPartitionsDefinition,
    Jitter,
    MaterializeResult,
    Output,
    RetryPolicy,
    asset,
    asset_check,
)

from pipeline_shared import (
    METRIC_NAMES,
    detect_anomalies_for_day,
    fetch_metric,
    refresh_derived_for_day,
    reparse_metric,
)
from pipeline_shared.garmin import _reparse_hr_samples, record_run

from pipeline_dagster_proj.resources import GarminPipelineResource

log = logging.getLogger(__name__)

# end_offset=1 so today's (in-progress) partition is a valid key. With the
# default end_offset=0, the most recent valid key is "yesterday" until UTC
# midnight rolls over, which makes the daily schedule's "today" RunRequest
# fail with DagsterUnknownPartitionError.
partitions = DailyPartitionsDefinition(start_date="2026-01-01", end_offset=1)

# Tag for Dagster's op-level concurrency pool — see dagster.yaml `concurrency.pools.garmin_api.limit: 1`.
# Limits how many Garmin-API-touching ops can run concurrently across ALL runs,
# protecting against parallel logins (the root cause of the 429 we tripped).
GARMIN_OP_TAGS = {"dagster/concurrency_key": "garmin_api"}

# Retry policy for raw assets — when Garmin returns 429 / login fails (the
# `mobile+cffi returned 429` cascade we saw), wait + retry rather than fail.
# 30s × 2 × 2 = 30 / 60 / 120 with jitter ⇒ ≤3.5 min of retries.
GARMIN_RETRY = RetryPolicy(
    max_retries=3,
    delay=30,
    backoff=Backoff.EXPONENTIAL,
    jitter=Jitter.PLUS_MINUS,
)


def _should_force_api(partition_key: str) -> bool:
    """Re-fetch from Garmin even when the cache has a row, for today and
    yesterday's partitions.

    Why: Garmin syncs sleep / hrv / stress data ~6 AM the day *after* the
    activity. A 22:00 UTC fetch of "today" gets only partial-day data; the
    next morning's 08:00 fire would normally see a cache hit and skip the
    API. By forcing the API for today + yesterday, each scheduled tick keeps
    those two partitions fresh. Older partitions are immutable and always
    take the cache fast-path.

    `force_api=True` also propagates to the stats sub-fetch inside
    fetch_metric, so stale `{}` doesn't poison the parser for empty
    sub-responses that have since been populated upstream.
    """
    target = date.fromisoformat(partition_key)
    today = date.today()
    return target >= today - timedelta(days=1)


def _make_raw_asset(metric_name: str):
    @asset(
        name=f"raw_{metric_name}",
        partitions_def=partitions,
        group_name="garmin_raw",
        op_tags=GARMIN_OP_TAGS,
        retry_policy=GARMIN_RETRY,
    )
    def _raw(context, garmin: GarminPipelineResource) -> Output:
        date_str = context.partition_key
        run = garmin.make_run(context.run_id)
        # Cache-first by default. For today + yesterday, bypass the cache and
        # hit the API so partial / late-arriving days (e.g. sleep finalising
        # at ~6 AM next day) actually refresh on each scheduled fire.
        force_api = _should_force_api(date_str)
        result = fetch_metric(run, date_str, metric_name, force_api=force_api)
        record_run(
            garmin.settings().database_url,
            run_id=context.run_id, tool=garmin.tool,
            asset=f"raw_{metric_name}", partition_key=date_str,
            status=result.status, started_at=datetime.now(timezone.utc),
            metadata={"source": result.source, "parsed_rows": result.parsed_rows,
                     "error": result.error},
        )
        return Output(
            value={"status": result.status, "parsed_rows": result.parsed_rows},
            metadata={
                "status": result.status, "source": result.source,
                "parsed_rows": result.parsed_rows,
                "error": result.error or "",
            },
        )
    return _raw


# 7 raw assets — one per metric
raw_assets = [_make_raw_asset(m) for m in METRIC_NAMES]


@asset(
    partitions_def=partitions,
    group_name="garmin_raw",
    op_tags=GARMIN_OP_TAGS,
    retry_policy=GARMIN_RETRY,
)
def raw_heart_rate_samples(
    context, garmin: GarminPipelineResource
) -> Output:
    date_str = context.partition_key
    run = garmin.make_run(context.run_id)
    r = _reparse_hr_samples(run, date_str)
    record_run(
        garmin.settings().database_url,
        run_id=context.run_id, tool=garmin.tool,
        asset="raw_heart_rate_samples", partition_key=date_str,
        status=r.status, started_at=datetime.now(timezone.utc),
        metadata={"parsed_rows": r.parsed_rows, "error": r.error},
    )
    return Output(
        value={"status": r.status, "samples": r.parsed_rows},
        metadata={"status": r.status, "samples": r.parsed_rows},
    )


@asset(
    partitions_def=partitions,
    group_name="garmin_derived",
    deps=[a.key for a in raw_assets] + [raw_heart_rate_samples.key],
)
def derived_daily(
    context, garmin: GarminPipelineResource
) -> Output:
    date_str = context.partition_key
    counts = refresh_derived_for_day(garmin.settings().database_url, date_str)
    record_run(
        garmin.settings().database_url,
        run_id=context.run_id, tool=garmin.tool,
        asset="derived_daily", partition_key=date_str,
        status="ok", started_at=datetime.now(timezone.utc),
        metadata=counts,
    )
    return Output(value=counts, metadata=counts)


@asset(
    partitions_def=partitions,
    group_name="garmin_alerts",
    deps=[derived_daily.key],
)
def anomaly_candidates(
    context, garmin: GarminPipelineResource
) -> Output:
    date_str = context.partition_key
    dets = detect_anomalies_for_day(garmin.settings().database_url, date_str)
    return Output(
        value={"count": len(dets), "items": dets},
        metadata={"count": len(dets), "kinds": [d["kind"] for d in dets]},
    )


# --- asset checks: port of validate.py ----------------------------------

def _make_validation_check(asset_name: str, table: str,
                            columns: list[tuple[str, bool]]):
    """Build an @asset_check for a parsed asset.

    `columns`: list of (column_name, allow_zero).
    """
    @asset_check(name=f"validate_{table}", asset=asset_name)
    def _check(garmin: GarminPipelineResource) -> AssetCheckResult:
        import psycopg
        issues: list[str] = []
        with psycopg.connect(garmin.settings().database_url) as conn:
            with conn.cursor() as cur:
                for col, allow_zero in columns:
                    cur.execute(f"SELECT {col} FROM {table}")
                    rows = cur.fetchall()
                    null_count = sum(1 for r in rows if r[0] is None)
                    zero_count = (sum(1 for r in rows if r[0] == 0)
                                  if not allow_zero else 0)
                    if null_count:
                        issues.append(f"{col} NULL ×{null_count}")
                    if zero_count:
                        issues.append(f"{col} ZERO ×{zero_count}")
        return AssetCheckResult(passed=not issues, metadata={"issues": issues})
    return _check


validate_sleep = _make_validation_check(
    "raw_sleep", "sleep",
    [("sleep_score", False), ("duration_secs", False), ("qualifier", False)],
)
validate_heart_rate = _make_validation_check(
    "raw_heart_rate", "heart_rate",
    [("resting", False), ("max", False), ("min", False)],
)
validate_hrv = _make_validation_check(
    "raw_hrv", "hrv",
    [("last_night_avg", False), ("weekly_avg", False)],
)
