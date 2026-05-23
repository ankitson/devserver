"""Banking pipeline in Dagster.

Pattern: two jobs + one sensor (the eval doc's recommended Dagster approach
for human-in-the-loop, since Dagster lacks `awakeable`):

  Job A — `bank_login_job`:
    op 1: insert bank_imports row, status='awaiting_login'
    op 2: drive Playwright to OTP page, capture cookies
    op 3: store cookies + masked + workflow_handle in `bank_pending` table,
           enqueue OTP notification, status='awaiting_otp'

  Sensor — `bank_otp_sensor` (poll interval = 5s):
    finds bank_pending rows where otp IS NOT NULL, kicks off Job B per row

  Job B — `bank_resume_job`:
    op 1: read bank_pending row by id
    op 2: re-launch browser with cookies, submit OTP, download CSV
    op 3: process CSV → transactions
    op 4: mark bank_pending row done, update bank_imports status='ok'
"""

import json
import logging
from datetime import datetime, timezone

import psycopg
from dagster import (
    AssetExecutionContext,
    DagsterRunStatus,
    DefaultSensorStatus,
    Definitions,
    OpExecutionContext,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    define_asset_job,
    job,
    op,
    sensor,
)

from pipeline_shared import (
    BankImportRequest,
    Notifier,
    bank_login_and_pause,
    bank_resume_and_download,
    process_statement_csv,
)
from pipeline_shared.config import Settings

from pipeline_dagster_proj.resources import GarminPipelineResource

log = logging.getLogger(__name__)


_BANK_PENDING_SQL = """
CREATE TABLE IF NOT EXISTS bank_pending (
    id            BIGSERIAL PRIMARY KEY,
    bank_name     TEXT NOT NULL,
    username      TEXT NOT NULL,
    password      TEXT NOT NULL,
    storage_state JSONB NOT NULL,
    masked_destination TEXT,
    otp           TEXT,
    status        TEXT NOT NULL DEFAULT 'awaiting_otp',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);
"""


def _ensure_pending_table(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_BANK_PENDING_SQL)


@op(required_resource_keys={"garmin"})
def bank_login_op(context) -> int:
    """Phase 1: open browser → reach OTP page → save cookies → return pending_id.
    The trigger config carries bank_name/username/password."""
    import asyncio
    cfg = context.op_config or {}
    bank_name = cfg.get("bank_name", "mock-bank")
    username = cfg.get("username", "ankit")
    password = cfg.get("password", "test")

    garmin: GarminPipelineResource = context.resources.garmin
    s = garmin.settings()
    _ensure_pending_table(s.database_url)

    req = BankImportRequest(bank_name=bank_name, username=username, password=password)
    result = asyncio.run(bank_login_and_pause(settings=s, request=req))
    storage_state = result["storage_state"]
    masked = result["masked_destination"]

    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank_imports (bank_name, status)
                VALUES (%s, 'awaiting_otp')
                RETURNING id
                """,
                (bank_name,),
            )
            import_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO bank_pending
                    (bank_name, username, password, storage_state,
                     masked_destination, status)
                VALUES (%s, %s, %s, %s::jsonb, %s, 'awaiting_otp')
                RETURNING id
                """,
                (bank_name, username, password, json.dumps(storage_state), masked),
            )
            pending_id = cur.fetchone()[0]
    prompt_url = (
        f"{s.mock_bank_url.rstrip('/')}/dagster-approve?pending_id={pending_id}"
    )
    Notifier(s).enqueue(
        kind="bank:otp_required", severity="warn",
        title=f"OTP required for {bank_name}",
        body=f"Bank sent OTP to {masked}. Approve at: {prompt_url}",
        payload={"pending_id": pending_id, "bank": bank_name, "masked": masked,
                 "prompt_url": prompt_url, "import_id": import_id},
    )
    context.log.info("bank_pending id=%s awaiting OTP", pending_id)
    return pending_id


bank_login_op = bank_login_op.configured(
    {"bank_name": "mock-bank", "username": "ankit", "password": "test"},
    name="bank_login_op_default",
)


@op(required_resource_keys={"garmin"})
def bank_resume_op(context, pending_id: int) -> dict:
    """Phase 2: read pending row, re-open browser with cookies, submit OTP,
    download CSV, process into transactions, mark pending row done."""
    import asyncio
    garmin: GarminPipelineResource = context.resources.garmin
    s = garmin.settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bank_name, username, password, storage_state, otp
                  FROM bank_pending WHERE id = %s
                """,
                (pending_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"no pending row {pending_id}")
            bank_name, username, password, storage_state, otp = row
            if not otp:
                raise RuntimeError(f"pending {pending_id} has no OTP yet")
    req = BankImportRequest(bank_name=bank_name, username=username, password=password)
    statement_path = asyncio.run(bank_resume_and_download(
        settings=s, request=req,
        storage_state=storage_state if isinstance(storage_state, dict)
                      else json.loads(storage_state),
        otp=otp, download_path_prefix=f"{bank_name}-dagster-{pending_id}",
    ))
    counts = process_statement_csv(
        database_url=s.database_url, csv_path=statement_path, bank_name=bank_name,
    )
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bank_pending
                   SET status='completed', completed_at=NOW()
                 WHERE id=%s
                """,
                (pending_id,),
            )
            cur.execute(
                """
                UPDATE bank_imports
                   SET status='ok', finished_at=NOW(),
                       txn_count=%s, statement_file=%s
                 WHERE id = (SELECT MAX(id) FROM bank_imports WHERE bank_name=%s)
                """,
                (counts["inserted"] + counts["updated"], statement_path, bank_name),
            )
    return {"pending_id": pending_id, "counts": counts,
            "statement_path": statement_path}


# --- jobs ----------------------------------------------------------------

@job
def bank_login_job():
    bank_login_op()


@job
def bank_resume_job():
    bank_resume_op()


# --- sensor for OTP arrival ---------------------------------------------

@sensor(
    job=bank_resume_job, minimum_interval_seconds=3,
    default_status=DefaultSensorStatus.RUNNING,
)
def bank_otp_sensor(context: SensorEvaluationContext) -> SensorResult:
    """Poll bank_pending; when a row's OTP is filled, fire bank_resume_job for it."""
    import os
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return SensorResult([])
    _ensure_pending_table(db_url)
    cursor = context.cursor or "0"
    last_seen = int(cursor)
    requests = []
    new_last = last_seen
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM bank_pending
                 WHERE id > %s AND otp IS NOT NULL AND status = 'awaiting_otp'
                 ORDER BY id LIMIT 10
                """,
                (last_seen,),
            )
            ids = [r[0] for r in cur.fetchall()]
    for pid in ids:
        requests.append(RunRequest(
            run_key=f"bank-resume-{pid}",
            run_config={"ops": {"bank_resume_op": {"inputs": {"pending_id": pid}}}},
        ))
        if pid > new_last:
            new_last = pid
            # mark this row as picked up so future polls don't re-fire
            with psycopg.connect(db_url, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE bank_pending SET status='processing' WHERE id=%s",
                        (pid,),
                    )
    return SensorResult(requests, cursor=str(new_last))


# --- notifier drain sensor ----------------------------------------------

@sensor(
    minimum_interval_seconds=30, default_status=DefaultSensorStatus.RUNNING,
)
def notifier_sensor(context: SensorEvaluationContext) -> SensorResult:
    import os
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return SensorResult([])
    from pipeline_shared.config import load_settings
    Notifier(load_settings()).drain_once()
    return SensorResult([])
