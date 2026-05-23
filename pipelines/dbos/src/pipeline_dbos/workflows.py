"""DBOS workflows for the Garmin and banking pipelines.

Garmin: scheduled daily tick + on-demand reparse + per-day workflow.
Banking: one-shot import workflow with browser automation + OTP via DBOS.recv.
Notifier: scheduled drain of the notifications queue every minute.

Conservative defaults:
  - Garmin live fetch defaults off (GARMIN_LIVE_FETCH=false). reparse-from-cache
    is the documented path.
  - Each Garmin metric is a separate step inside the day workflow so DBOS
    replay only re-runs failed metrics on retry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from dbos import DBOS

from pipeline_shared import (
    BankImportRequest,
    GarminRun,
    METRIC_NAMES,
    Notifier,
    bank_login_and_pause,
    bank_resume_and_download,
    detect_anomalies_for_day,
    load_settings,
    process_statement_csv,
    refresh_derived_for_day,
    reparse_metric,
)
from pipeline_shared.garmin import _reparse_hr_samples, record_run

from pipeline_dbos.awaiter import DbosOtpAwaiter

log = logging.getLogger(__name__)
TOOL = "dbos"


def _settings():
    return load_settings()


def _new_run() -> GarminRun:
    s = _settings()
    return GarminRun(
        settings=s,
        target_url=s.database_url,
        source_url=s.garmin_source_database_url,
        tool=TOOL,
        run_id=DBOS.workflow_id or str(uuid.uuid4()),
    )


# --- Garmin: per-metric step (replay-safe via DBOS journal) ---------------

@DBOS.step(retries_allowed=True, max_attempts=4, interval_seconds=2.0, backoff_rate=2.0)
def reparse_metric_step(date_str: str, metric: str) -> dict:
    run = _new_run()
    r = reparse_metric(run, date_str, metric)
    return {
        "metric": r.metric, "status": r.status, "source": r.source,
        "parsed_rows": r.parsed_rows, "error": r.error,
    }


@DBOS.step(retries_allowed=True, max_attempts=4, interval_seconds=2.0, backoff_rate=2.0)
def reparse_hr_samples_step(date_str: str) -> dict:
    run = _new_run()
    r = _reparse_hr_samples(run, date_str)
    return {
        "metric": r.metric, "status": r.status,
        "parsed_rows": r.parsed_rows, "error": r.error,
    }


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2.0)
def refresh_derived_step(date_str: str) -> dict:
    s = _settings()
    return refresh_derived_for_day(s.database_url, date_str)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2.0)
def detect_anomalies_step(date_str: str) -> list[dict]:
    s = _settings()
    return detect_anomalies_for_day(s.database_url, date_str)


# --- Garmin: per-day workflow ---------------------------------------------

@DBOS.workflow()
def fetch_day_workflow(date_str: str) -> dict:
    """Reparse all 7 metrics + heart_rate_samples for one date.

    Each metric is a separate durable step → individual retry on failure.
    Downstream derive + detect run only if at least one metric upserted.
    """
    started = datetime.now(timezone.utc)
    results: dict[str, dict] = {}
    for metric in METRIC_NAMES:
        results[metric] = reparse_metric_step(date_str, metric)
    results["heart_rate_samples"] = reparse_hr_samples_step(date_str)
    any_ok = any(r.get("status") == "ok" for r in results.values())
    if any_ok:
        results["derived"] = refresh_derived_step(date_str)
        results["anomalies"] = detect_anomalies_step(date_str)
    finished = datetime.now(timezone.utc)
    record_run(
        _settings().database_url,
        run_id=DBOS.workflow_id, tool=TOOL,
        asset="fetch_day", partition_key=date_str,
        status="ok" if any_ok else "no_data",
        started_at=started, finished_at=finished,
        metadata={"results": results},
    )
    return {"date": date_str, "results": results}


@DBOS.workflow()
def fetch_window_workflow(start: str, end: str) -> dict:
    """Reparse a date range — one fetch_day_workflow per date.

    Each child workflow has its own durability boundary — if the window
    crashes on day N, the worker restart resumes from day N.
    """
    s_date = date.fromisoformat(start)
    e_date = date.fromisoformat(end)
    handles = []
    d = s_date
    while d <= e_date:
        h = DBOS.start_workflow(fetch_day_workflow, d.isoformat())
        handles.append((d.isoformat(), h))
        d += timedelta(days=1)
    out = {}
    for dstr, h in handles:
        out[dstr] = h.get_result()
    return {"start": start, "end": end, "days": out}


# --- Garmin: scheduled tick ----------------------------------------------

@DBOS.scheduled("0 8,14,22 * * *")
@DBOS.workflow()
def garmin_daily_tick(scheduled_time: datetime, actual_time: datetime) -> dict:
    today = date.today()
    yesterday = today - timedelta(days=1)
    DBOS.start_workflow(fetch_day_workflow, today.isoformat())
    cutoff_hour = 10
    if actual_time.hour < cutoff_hour:
        DBOS.start_workflow(fetch_day_workflow, yesterday.isoformat())
    return {"scheduled": scheduled_time.isoformat(), "actual": actual_time.isoformat()}


# --- Notifier: scheduled drain -------------------------------------------

@DBOS.scheduled("* * * * *")
@DBOS.workflow()
def notifier_tick(scheduled_time: datetime, actual_time: datetime) -> dict:
    pushed = _notifier_drain_step()
    return {"pushed": pushed}


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
def _notifier_drain_step() -> int:
    return Notifier(_settings()).drain_once()


# --- Banking: workflow + supporting steps --------------------------------

@DBOS.step(max_attempts=2)
async def _bank_login_step(req_dict: dict) -> dict:
    """Phase 1: drive browser to bank OTP page, save cookies, close browser."""
    s = _settings()
    req = BankImportRequest(
        bank_name=req_dict["bank_name"],
        username=req_dict["username"],
        password=req_dict["password"],
    )
    return await bank_login_and_pause(settings=s, request=req)


@DBOS.step(max_attempts=2)
async def _bank_download_step(
    req_dict: dict, storage_state: dict, otp: str, wf_id: str
) -> str:
    """Phase 2: re-launch browser with prior cookies, submit OTP, download CSV."""
    s = _settings()
    req = BankImportRequest(
        bank_name=req_dict["bank_name"],
        username=req_dict["username"],
        password=req_dict["password"],
    )
    return await bank_resume_and_download(
        settings=s, request=req, storage_state=storage_state, otp=otp,
        download_path_prefix=f"{req_dict['bank_name']}-{wf_id}",
    )


@DBOS.step()
async def _enqueue_otp_request_step(bank_name: str, prompt_url: str,
                                    masked: str) -> None:
    s = _settings()
    Notifier(s).enqueue(
        kind="bank:otp_required", severity="warn",
        title=f"OTP required for {bank_name}",
        body=f"Bank sent OTP to {masked}. Open {prompt_url} to enter it.",
        payload={"bank": bank_name, "workflow_id": DBOS.workflow_id,
                 "prompt_url": prompt_url, "masked": masked},
    )


@DBOS.step()
async def _bank_process_csv_step(csv_path: str, bank_name: str) -> dict:
    s = _settings()
    return process_statement_csv(
        database_url=s.database_url,
        csv_path=csv_path,
        bank_name=bank_name,
    )


@DBOS.step()
async def _bank_log_start_step(bank_name: str) -> int:
    s = _settings()
    import psycopg
    with psycopg.connect(s.database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bank_imports (bank_name, status) VALUES (%s, 'running') RETURNING id",
            (bank_name,),
        )
        return cur.fetchone()[0]


@DBOS.step()
async def _bank_log_finish_step(
    import_id: int, status: str, txn_count: int | None,
    statement_file: str | None, error: str | None,
) -> None:
    s = _settings()
    import psycopg
    with psycopg.connect(s.database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bank_imports
               SET finished_at = NOW(), status = %s, txn_count = %s,
                   statement_file = %s, error = %s
             WHERE id = %s
            """,
            (status, txn_count, statement_file, error, import_id),
        )


# --- X (Twitter) bookmarks ----------------------------------------------


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=60.0)
def fetch_x_bookmarks_step(pages: int, no_threads: bool, no_quotes: bool) -> dict:
    """A single durable step that pulls one or more bookmark pages.

    Rate limited via pipeline_shared._wait_for_bookmark_rate_limit (16 min
    floor between page calls). The DBOS step's own retry policy is 1 min
    apart, capped at 3 attempts — for the case where X returns 5xx, NOT for
    rate-limit recovery.
    """
    import os
    from pipeline_shared import fetch_and_store_bookmarks
    from pipeline_shared.x_bookmarks import XClientConfig
    s = _settings()
    cfg = XClientConfig(
        client_id=os.environ["X_OAUTH_CLIENT_ID"],
        client_secret=os.environ["X_OAUTH_CLIENT_SECRET"],
        redirect_uri=os.environ.get(
            "X_OAUTH_REDIRECT_URI", "http://localhost:18801/x-callback"
        ),
        account=os.environ.get("X_ACCOUNT", "default"),
    )
    rate = float(os.environ.get("X_BOOKMARK_RATE_LIMIT_SECONDS", "960"))
    return fetch_and_store_bookmarks(
        settings=s, cfg=cfg, pages=pages, rate_limit_seconds=rate,
        resolve_threads=not no_threads, resolve_quotes=not no_quotes,
    )


@DBOS.workflow()
def fetch_x_bookmarks_workflow(
    pages: int = 1, no_threads: bool = False, no_quotes: bool = False,
) -> dict:
    return fetch_x_bookmarks_step(pages, no_threads, no_quotes)


# Scheduled — once an hour by default. X bookmark endpoint rate limit makes
# anything faster pointless; the actual API call is gated by the in-process
# limiter inside fetch_and_store_bookmarks too.
@DBOS.scheduled("0 * * * *")
@DBOS.workflow()
def x_bookmarks_hourly(scheduled_time, actual_time) -> dict:
    try:
        return fetch_x_bookmarks_step(pages=1, no_threads=False, no_quotes=False)
    except Exception as e:  # noqa: BLE001
        log.warning("x_bookmarks_hourly failed (likely no tokens yet): %s", e)
        return {"status": "skipped", "error": str(e)}


@DBOS.workflow()
async def import_bank_statement(
    bank_name: str,
    username: str,
    password: str,
    otp_timeout_seconds: int = 3600,
) -> dict:
    """End-to-end bank import — async workflow.
    1. Audit row in bank_imports.
    2. Step: drive browser to OTP page, save cookies, return state.
    3. Workflow body: notify human, await DBOS.recv_async for OTP.
    4. Step: re-launch browser with cookies, submit OTP, download CSV.
    5. Step: process CSV into transactions.
    """
    s = _settings()
    import_id = await _bank_log_start_step(bank_name)
    req_dict = {"bank_name": bank_name, "username": username, "password": password}
    prompt_url = f"{s.mock_bank_url.rstrip('/')}/approve?wf={DBOS.workflow_id}"
    try:
        # Phase 1 — login, get to OTP page, save cookies.
        login_result = await _bank_login_step(req_dict)
        await _enqueue_otp_request_step(
            bank_name, prompt_url, login_result["masked_destination"]
        )
        # Phase 2 — durable wait inside the workflow body (recv allowed here).
        otp = await DBOS.recv_async("otp", timeout_seconds=otp_timeout_seconds)
        if not otp:
            await _bank_log_finish_step(import_id, "timeout", None, None,
                                         "OTP not received")
            return {"status": "timeout"}
        # Phase 3 — re-open browser, submit OTP, download.
        statement_path = await _bank_download_step(
            req_dict, login_result["storage_state"], otp, DBOS.workflow_id,
        )
        # Phase 4 — process the CSV into transactions.
        counts = await _bank_process_csv_step(statement_path, bank_name)
        await _bank_log_finish_step(
            import_id, "ok", counts["inserted"] + counts["updated"],
            statement_path, None,
        )
        return {"status": "ok", "counts": counts, "statement": statement_path}
    except Exception as e:
        await _bank_log_finish_step(import_id, "error", None, None, str(e))
        raise
