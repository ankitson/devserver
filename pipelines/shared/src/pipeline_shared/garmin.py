"""Run-scoped Garmin fetch helpers.

Strict rules baked in here (not overridable by callers):

1. **Conservative rate limit**: enforced via a process-wide gate. Default 2.0s
   between calls (vs garmin-fetch's own 1.0s floor) when live fetching is on.
2. **Live fetch off by default**: settings.garmin_live_fetch must be True for
   any real API call. Otherwise everything reads from raw_responses cache.
3. **Raw response always stored first**: on any successful API call, we write to
   raw_responses *before* attempting parse/upsert. If parse fails, raw is still
   preserved for later reparsing.
4. **Run-scoped client**: a `GarminRun` context binds one GarminClient.login()
   to the lifetime of a fetch — never log in per-metric.
5. **Reparse is the default workflow**: callers should prefer
   reparse_day/reparse_metric when raw data is present.

This module intentionally returns simple status dicts so orchestrators (Dagster
assets, DBOS workflows, Restate handlers) can record results uniformly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import psycopg

from garmin_fetch.client import GarminClient
from garmin_fetch.fetcher import (
    FETCHERS,
    _get_activity_windows,
    _parse_body_battery,
    _parse_heart_rate,
    _parse_heart_rate_samples,
    _parse_hrv,
    _parse_sleep,
    _parse_steps,
    _parse_stress,
    _parse_training_readiness,
)
from garmin_fetch.store import GarminStore

from pipeline_shared.config import Settings

log = logging.getLogger(__name__)

METRIC_NAMES: tuple[str, ...] = tuple(FETCHERS.keys())

PARSERS: dict[str, Callable] = {
    "sleep": _parse_sleep,
    "heart_rate": _parse_heart_rate,
    "hrv": _parse_hrv,
    "stress": _parse_stress,
    "body_battery": _parse_body_battery,
    "steps": _parse_steps,
    "training_readiness": _parse_training_readiness,
}

NEEDS_STATS = {"sleep", "stress", "steps"}


class _RateLimiter:
    """Process-wide minimum spacing between calls. Thread-safe.

    Layered on top of GarminClient._rate_limit (1s) — gives us a deliberately
    higher floor when live fetching is enabled.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            wait_s = self.min_interval - elapsed
            if wait_s > 0:
                time.sleep(wait_s)
            self._last = time.monotonic()


_LIMITERS: dict[float, _RateLimiter] = {}


def _limiter(interval: float) -> _RateLimiter:
    if interval not in _LIMITERS:
        _LIMITERS[interval] = _RateLimiter(interval)
    return _LIMITERS[interval]


@dataclass
class FetchResult:
    metric: str
    date_str: str
    status: str  # "ok", "skipped", "no_data", "cached", "error: ..."
    source: str  # "api", "cache", "skipped"
    parsed_rows: int = 0
    raw_bytes: int = 0
    error: str | None = None


@dataclass
class GarminRun:
    """A run-scoped fetch context. One GarminClient login, many metric fetches.

    Use as a context manager. The client is created lazily on first live call,
    so reparse-only runs never log in.

    Always uses 2 separate DB connections:
      - `target_store`: the pipeline's DB (parsed/derived/notifications go here).
      - `source_url`: read-only handle for fetching raw_responses (when the
                       pipeline's own raw_responses is empty, falls back to the
                       source garmin DB).
    """

    settings: Settings
    target_url: str
    source_url: str
    tool: str
    run_id: str
    _client: GarminClient | None = field(default=None, init=False, repr=False)
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @contextmanager
    def store(self) -> Iterator[GarminStore]:
        """Open the target GarminStore (creates tables on first use)."""
        store = GarminStore(self.target_url)
        try:
            yield store
        finally:
            store.close()

    def get_client(self) -> GarminClient:
        """Lazy-login, run-scoped, thread-safe.

        Uses python-garminconnect's native `tokenstore` parameter: on a fresh
        directory it does a full credential login (cascading through the
        library's 5-strategy chain — `mobile+cffi` / `widget+cffi` /
        `portal+cffi` / ...) and dumps the OAuth tokens to the path. Every
        subsequent run loads those tokens and proactively refreshes the DI
        token if expiring, skipping the credential flow entirely.

        Tokens live in `GARMIN_TOKEN_DIR` (default `/root/.garth`, mounted as
        a docker volume in the pipeline containers).

        Note on garth: upstream's mobile-auth helper is deprecated, but the
        library already falls through past `mobile+cffi` to `widget+cffi` /
        `portal+cffi`, and the OAuth refresh tokens written by any of those
        strategies are reusable via the same `tokenstore` mechanism.
        """
        import os
        if not self.settings.garmin_live_fetch:
            raise RuntimeError(
                "Live Garmin fetch is disabled (GARMIN_LIVE_FETCH=false). "
                "Use reparse_day / reparse_metric instead."
            )
        if not self.settings.has_garmin_creds():
            raise RuntimeError("GARMIN_EMAIL / GARMIN_PASSWORD not configured")
        with self._client_lock:
            if self._client is None:
                from garminconnect import Garmin
                token_dir = os.environ.get("GARMIN_TOKEN_DIR", "/root/.garth")
                os.makedirs(token_dir, exist_ok=True)
                token_files = [f for f in os.listdir(token_dir) if not f.startswith(".")]
                gc = Garmin(self.settings.garmin_email, self.settings.garmin_password)
                gc.login(tokenstore=token_dir)
                if token_files:
                    log.info("[garmin] login via cached tokens at %s", token_dir)
                else:
                    log.info("[garmin] fresh login; tokens dumped to %s", token_dir)
                c = GarminClient()
                c.client = gc  # bypass GarminClient._login — already authenticated
                self._client = c
            return self._client

    def rate_limit(self) -> None:
        _limiter(self.settings.garmin_min_request_interval_seconds).wait()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_raw_from_pipeline_or_source(
    target_store: GarminStore,
    source_url: str,
    date_str: str,
    metric: str,
) -> Any | None:
    """Look up raw_responses for (date, metric).

    Order:
      1. Try the pipeline's own raw_responses.
      2. If absent, look in the source garmin DB (the existing garmin-fetch
         cron job's accumulated cache) — and seed it into the pipeline DB.

    The source DB is optional/legacy: after the DB consolidation it may not
    exist. An unreachable source is treated as a cache-miss (returns None),
    not a fatal error — otherwise an uncached metric (e.g. `activities`) would
    crash the whole reparse, including the scheduled hr-samples asset.

    Returns the deserialized response, or None if not found anywhere.
    """
    found = target_store.get_raw_response(date_str, metric)
    if found is not None:
        return found
    if source_url == target_store.database_url:
        return None
    try:
        with psycopg.connect(source_url) as src:
            with src.cursor() as cur:
                cur.execute(
                    "SELECT response, fetched_at FROM raw_responses WHERE date=%s AND metric=%s",
                    (date_str, metric),
                )
                row = cur.fetchone()
    except psycopg.OperationalError as e:
        log.debug("source garmin DB unreachable (%s); treating as cache-miss", e)
        return None
    if not row:
        return None
    response_text, fetched_at = row
    target_store.store_raw_response(date_str, metric, json.loads(response_text))
    return json.loads(response_text)


def _parse_with_stats(metric: str, raw: Any, stats: Any | None) -> dict | None:
    parser = PARSERS[metric]
    if metric in NEEDS_STATS:
        return parser(raw, stats)
    return parser(raw)


def _upsert_parsed(store: GarminStore, metric: str, date_str: str, parsed: dict) -> int:
    """Call the appropriate GarminStore.upsert_* method. Returns 1 if upserted."""
    method_name = FETCHERS[metric][2]
    getattr(store, method_name)(date_str, parsed)
    return 1


def reparse_metric(
    run: GarminRun, date_str: str, metric: str
) -> FetchResult:
    """Reparse one (date, metric) from raw_responses. No API calls."""
    assert metric in METRIC_NAMES, f"unknown metric: {metric}"
    with run.store() as store:
        raw = _get_raw_from_pipeline_or_source(store, run.source_url, date_str, metric)
        if raw is None:
            return FetchResult(metric, date_str, "no_raw", "cache", error="no raw_responses row")
        stats = None
        if metric in NEEDS_STATS:
            stats = _get_raw_from_pipeline_or_source(store, run.source_url, date_str, "stats")
        try:
            parsed = _parse_with_stats(metric, raw, stats)
        except Exception as e:
            return FetchResult(metric, date_str, f"error: {e}", "cache", error=str(e))
        if parsed is None:
            return FetchResult(metric, date_str, "no_data", "cache")
        try:
            rows = _upsert_parsed(store, metric, date_str, parsed)
        except Exception as e:
            return FetchResult(metric, date_str, f"error: {e}", "cache", error=str(e))
        return FetchResult(metric, date_str, "ok", "cache", parsed_rows=rows)


def reparse_day(run: GarminRun, date_str: str) -> list[FetchResult]:
    """Reparse all 7 metrics + heart_rate_samples for one day from cache."""
    out: list[FetchResult] = []
    for metric in METRIC_NAMES:
        out.append(reparse_metric(run, date_str, metric))
    out.append(_reparse_hr_samples(run, date_str))
    return out


def _reparse_hr_samples(run: GarminRun, date_str: str) -> FetchResult:
    with run.store() as store:
        hr_raw = _get_raw_from_pipeline_or_source(store, run.source_url, date_str, "heart_rate")
        if hr_raw is None:
            return FetchResult(
                "heart_rate_samples", date_str, "no_raw", "cache",
                error="no heart_rate raw",
            )
        activities = _get_raw_from_pipeline_or_source(
            store, run.source_url, date_str, "activities"
        )
        try:
            samples = _parse_heart_rate_samples(hr_raw, activities)
        except Exception as e:
            return FetchResult(
                "heart_rate_samples", date_str, f"error: {e}", "cache", error=str(e)
            )
        if not samples:
            return FetchResult("heart_rate_samples", date_str, "no_data", "cache")
        store.upsert_heart_rate_samples(date_str, samples)
        return FetchResult(
            "heart_rate_samples", date_str, "ok", "cache", parsed_rows=len(samples),
        )


def fetch_metric(
    run: GarminRun,
    date_str: str,
    metric: str,
    *,
    force_api: bool = False,
) -> FetchResult:
    """Fetch one (date, metric).

    If `garmin_live_fetch` is False (default), this is identical to reparse_metric.
    If True and `force_api` is True, bypass cache and call the Garmin API.
    If True and `force_api` is False, prefer cache when present.
    """
    if not run.settings.garmin_live_fetch:
        return reparse_metric(run, date_str, metric)

    with run.store() as store:
        if not force_api:
            existing = store.get_raw_response(date_str, metric)
            if existing is not None:
                return reparse_metric(run, date_str, metric)

        # Live API path — strict rate limit, always store raw first.
        client = run.get_client()
        api_method = FETCHERS[metric][0]
        run.rate_limit()
        try:
            raw = getattr(client, api_method)(date_str)
        except Exception as e:
            return FetchResult(metric, date_str, f"error: {e}", "api", error=str(e))
        store.store_raw_response(date_str, metric, raw or {})

        # Stats is a dependency for sleep/stress/steps — fetch on demand.
        # When the caller asked us to force the API for the main metric
        # (e.g. today/yesterday partitions), force the stats sub-fetch too —
        # otherwise a previously-cached empty stats response would keep
        # poisoning the parser.
        stats = None
        if metric in NEEDS_STATS:
            cached_stats = (
                None if force_api else store.get_raw_response(date_str, "stats")
            )
            if cached_stats is None:
                run.rate_limit()
                try:
                    stats = client.get_stats(date_str)
                except Exception as e:
                    log.warning("stats fetch failed for %s: %s", date_str, e)
                if stats is not None:
                    store.store_raw_response(date_str, "stats", stats)
            else:
                stats = cached_stats

        try:
            parsed = _parse_with_stats(metric, raw, stats)
        except Exception as e:
            return FetchResult(metric, date_str, f"error: {e}", "api", error=str(e))
        if parsed is None:
            return FetchResult(metric, date_str, "no_data", "api")
        _upsert_parsed(store, metric, date_str, parsed)
        return FetchResult(metric, date_str, "ok", "api", parsed_rows=1)


# A day whose sleep row exists but has a NULL score has been fetched yet Garmin
# has no score for it. That's either a not-yet-synced gap (worth retrying) or a
# night the watch wasn't worn (will never have a score). We can't tell them
# apart, so we retry — but only if the last fetch is older than this, so a
# permanently-scoreless night isn't refetched on every single daily run.
HEAL_RETRY_NULL_HOURS = 60


def find_missing_sleep_days(database_url: str, dates: list[str]) -> list[str]:
    """Of the given dates, return those still worth a re-fetch.

    A day qualifies if it has no `sleep` row at all, or its row has a NULL
    `sleep_score` AND was last fetched more than HEAL_RETRY_NULL_HOURS ago.
    The all-null `dailySleepDTO` Garmin serves until the watch syncs is the
    signature this heals; the staleness guard stops permanently-scoreless
    nights from being refetched every day until they age out of the window.
    """
    missing: list[str] = []
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for date_str in dates:
            cur.execute(
                "SELECT sleep_score IS NULL,"
                " (fetched_at IS NULL OR fetched_at < now() - make_interval(hours => %s))"
                " FROM sleep WHERE date = %s",
                (HEAL_RETRY_NULL_HOURS, date_str),
            )
            row = cur.fetchone()
            if row is None:
                missing.append(date_str)        # never fetched
            elif row[0] and row[1]:
                missing.append(date_str)         # null score + stale fetch
    return missing


def heal_missing_days(
    run: GarminRun,
    database_url: str,
    *,
    window_days: int = 10,
    today: date | None = None,
) -> dict:
    """Force a live re-fetch of recent days that are still missing sleep data.

    Walks the window [today-window_days, today-1] (today is skipped — it's
    still in progress), and for each day with no sleep score, force-refetches
    all metrics + heart_rate_samples and refreshes the derived rollups. Days
    that already have data cost only a single SELECT — so steady-state is ~free
    and real API work happens only when there's an actual gap to fill.

    Requires `garmin_live_fetch`; otherwise returns without touching the API.
    """
    from pipeline_shared.derived import refresh_derived_for_day

    today = today or date.today()
    window = [
        (today - timedelta(days=n)).isoformat()
        for n in range(1, window_days + 1)
    ]
    if not run.settings.garmin_live_fetch:
        return {"window": window, "missing": [], "healed": [], "skipped": "live_fetch_off"}

    missing = find_missing_sleep_days(database_url, window)
    healed = []
    for date_str in missing:
        results = [fetch_metric(run, date_str, m, force_api=True) for m in METRIC_NAMES]
        results.append(_reparse_hr_samples(run, date_str))
        refresh_derived_for_day(database_url, date_str)
        ok = sum(1 for r in results if r.status == "ok")
        healed.append({"date": date_str, "ok": ok})
        log.info("healed %s (ok=%d)", date_str, ok)
    return {"window": window, "missing": missing, "healed": healed}


def record_run(
    target_url: str,
    *,
    run_id: str,
    tool: str,
    asset: str,
    partition_key: str | None,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Append a row to pipeline_runs. Best-effort; never raises into caller."""
    try:
        finished = finished_at or _now()
        dur_ms = int((finished - started_at).total_seconds() * 1000)
        with psycopg.connect(target_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_runs
                        (run_id, tool, asset, partition_key, status,
                         started_at, finished_at, duration_ms, error, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        run_id, tool, asset, partition_key, status,
                        started_at, finished, dur_ms, error,
                        json.dumps(metadata) if metadata else None,
                    ),
                )
    except Exception as e:  # noqa: BLE001
        log.warning("record_run failed: %s", e)
