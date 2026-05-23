"""Restate handler definitions for Garmin and banking pipelines.

Restate's strengths used here:
  - `ctx.run(name, fn)` — durable steps; reparse_metric becomes one journaled step
  - `ctx.awakeable()` — durable pause for human OTP, no resident process
  - Per-invocation idempotency by id

Garmin ingest is a regular service (handlers are functions). Banking is a
Workflow (one-shot, addressable by id). Notifier is a periodic invocation.
"""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import restate
from restate import Service, Workflow

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

log = logging.getLogger(__name__)
TOOL = "restate"


def _settings():
    return load_settings()


def _make_run(run_id: str) -> GarminRun:
    s = _settings()
    return GarminRun(
        settings=s, target_url=s.database_url,
        source_url=s.garmin_source_database_url,
        tool=TOOL, run_id=run_id,
    )


garmin_ingest = Service("GarminIngest")


@garmin_ingest.handler()
async def fetch_day(ctx, date_str: str) -> dict:
    """Reparse all metrics for one date as separate durable steps."""
    run_id = ctx.request().id
    started = datetime.now(timezone.utc)
    results: dict[str, dict] = {}
    for metric in METRIC_NAMES:
        r = await ctx.run(
            f"reparse_{metric}",
            lambda m=metric, ds=date_str: _reparse_metric_sync(m, ds, run_id),
        )
        results[metric] = r
    results["heart_rate_samples"] = await ctx.run(
        "reparse_hr_samples",
        lambda: _reparse_hr_samples_sync(date_str, run_id),
    )
    any_ok = any(r.get("status") == "ok" for r in results.values())
    if any_ok:
        results["derived"] = await ctx.run(
            "derived", lambda: refresh_derived_for_day(_settings().database_url, date_str)
        )
        results["anomalies"] = await ctx.run(
            "anomalies", lambda: detect_anomalies_for_day(_settings().database_url, date_str)
        )
    finished = datetime.now(timezone.utc)
    await ctx.run("record_run", lambda: record_run(
        _settings().database_url, run_id=run_id, tool=TOOL,
        asset="fetch_day", partition_key=date_str,
        status="ok" if any_ok else "no_data",
        started_at=started, finished_at=finished, metadata=results,
    ))
    return {"date": date_str, "results": results}


@garmin_ingest.handler()
async def fetch_window(ctx, start_end: dict) -> dict:
    """fetch_day called per date in range; each via service_send so they run as
    independent durable invocations."""
    s_str, e_str = start_end["start"], start_end["end"]
    s_date = date.fromisoformat(s_str)
    e_date = date.fromisoformat(e_str)
    out = {}
    d = s_date
    while d <= e_date:
        r = await ctx.service_call(fetch_day, d.isoformat())
        out[d.isoformat()] = r
        d += timedelta(days=1)
    return {"start": s_str, "end": e_str, "days": out}


def _reparse_metric_sync(metric: str, date_str: str, run_id: str) -> dict:
    run = _make_run(run_id)
    r = reparse_metric(run, date_str, metric)
    return {"metric": r.metric, "status": r.status, "source": r.source,
            "parsed_rows": r.parsed_rows, "error": r.error}


def _reparse_hr_samples_sync(date_str: str, run_id: str) -> dict:
    run = _make_run(run_id)
    r = _reparse_hr_samples(run, date_str)
    return {"metric": r.metric, "status": r.status,
            "parsed_rows": r.parsed_rows, "error": r.error}


# --- banking workflow ---------------------------------------------------

bank_import = Workflow("BankImport")


@bank_import.main()
async def run(ctx, request: dict) -> dict:
    """End-to-end bank import as a Restate workflow.

    Steps:
      1. log_start — bank_imports row.
      2. login — open browser to OTP page, return cookies + masked.
      3. notify + create awakeable, wait for OTP value.
      4. resume — re-open browser with cookies, submit OTP, download.
      5. process — parse CSV → transactions.
      6. log_finish.
    """
    s = _settings()
    bank_name = request["bank_name"]
    username = request["username"]
    password = request["password"]
    workflow_id = ctx.key()

    import_id = await ctx.run("log_start", lambda: _log_start(bank_name))
    try:
        login_result = await ctx.run(
            "login", lambda: _login_sync(bank_name, username, password),
        )
        otp_id, otp_promise = ctx.awakeable(type_hint=str)
        prompt_url = (
            f"{s.mock_bank_url.rstrip('/')}/restate-approve?otp_id={otp_id}"
        )
        await ctx.run("notify", lambda: _enqueue_notification(
            bank_name, login_result["masked_destination"], prompt_url, workflow_id,
        ))
        otp = await otp_promise
        statement_path = await ctx.run(
            "resume", lambda: _resume_sync(
                bank_name, username, password,
                login_result["storage_state"], otp, workflow_id,
            ),
        )
        counts = await ctx.run("process_csv", lambda: process_statement_csv(
            database_url=s.database_url, csv_path=statement_path,
            bank_name=bank_name,
        ))
        await ctx.run("log_finish_ok", lambda: _log_finish(
            import_id, "ok", counts["inserted"] + counts["updated"],
            statement_path, None,
        ))
        return {"status": "ok", "counts": counts, "statement": statement_path}
    except Exception as e:
        await ctx.run("log_finish_err", lambda: _log_finish(
            import_id, "error", None, None, str(e),
        ))
        raise


def _login_sync(bank_name: str, username: str, password: str) -> dict:
    s = _settings()
    return asyncio.run(bank_login_and_pause(
        settings=s, request=BankImportRequest(
            bank_name=bank_name, username=username, password=password,
        ),
    ))


def _resume_sync(
    bank_name: str, username: str, password: str,
    storage_state: dict, otp: str, wf_id: str,
) -> str:
    s = _settings()
    return asyncio.run(bank_resume_and_download(
        settings=s, request=BankImportRequest(
            bank_name=bank_name, username=username, password=password,
        ),
        storage_state=storage_state, otp=otp,
        download_path_prefix=f"{bank_name}-restate-{wf_id}",
    ))


def _enqueue_notification(bank_name: str, masked: str, prompt_url: str, wf_id: str) -> None:
    s = _settings()
    Notifier(s).enqueue(
        kind="bank:otp_required", severity="warn",
        title=f"OTP required for {bank_name}",
        body=f"Bank sent OTP to {masked}. Open {prompt_url} to enter it.",
        payload={"workflow_id": wf_id, "bank": bank_name,
                 "masked": masked, "prompt_url": prompt_url},
    )


def _log_start(bank_name: str) -> int:
    import psycopg
    s = _settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bank_imports (bank_name, status) "
            "VALUES (%s, 'running') RETURNING id",
            (bank_name,),
        )
        return cur.fetchone()[0]


def _log_finish(
    import_id: int, status: str, txn_count: int | None,
    statement_file: str | None, error: str | None,
) -> None:
    import psycopg
    s = _settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bank_imports SET finished_at = NOW(), status = %s,
                txn_count = %s, statement_file = %s, error = %s
            WHERE id = %s
            """,
            (status, txn_count, statement_file, error, import_id),
        )


# --- notifier service ----------------------------------------------------

notifier_svc = Service("Notifier")


@notifier_svc.handler()
async def drain(ctx) -> dict:
    pushed = await ctx.run("drain", lambda: Notifier(_settings()).drain_once())
    return {"pushed": pushed}


@notifier_svc.handler()
async def tick(ctx) -> dict:
    """Self-rearming tick — called periodically by an external scheduler.

    Restate has cron support (cron_schedule) but we keep this simple.
    """
    return await drain(ctx)


# --- pyrestate app composition ------------------------------------------

restate_app = restate.app([garmin_ingest, bank_import, notifier_svc])
