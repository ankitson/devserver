"""Import recent Hacker News items for configured users.

The source is ClickHouse's attachable `hackernews_history` dataset, backed by
the public S3 disk from ClickHouse/ClickHouse#29693. The op uses
`clickhouse local` so no separate ClickHouse server is required in the pipeline
container.
"""

from __future__ import annotations

import json
import os
import subprocess
from html import unescape
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from dagster import DefaultScheduleStatus, RunRequest, RetryPolicy, job, op, schedule

from pipeline_shared.config import load_settings
from pipeline_shared.schema import ensure_schema


CREATE_HN_TABLE_SQL = """
CREATE TABLE hackernews_history UUID '259cf9f0-0c4f-451d-9029-7de661f9a085'
(
    update_time DateTime DEFAULT now(),
    id UInt32,
    deleted UInt8,
    type Enum8('story'=1,'comment'=2,'poll'=3,'pollopt'=4,'job'=5),
    by LowCardinality(String),
    time DateTime,
    text String,
    dead UInt8,
    parent UInt32,
    poll UInt32,
    kids Array(UInt32),
    url String,
    score Int32,
    title String,
    parts Array(UInt32),
    descendants Int32
)
ENGINE = ReplacingMergeTree(update_time)
ORDER BY id
SETTINGS refresh_parts_interval = 60,
    disk = disk(
        readonly = true,
        type = 's3_plain_rewritable',
        endpoint = 'https://clicklake-test-2.s3.eu-central-1.amazonaws.com/',
        use_environment_credentials = false
    )
"""


SELECT_USER_ITEMS_SQL = """
SELECT *
FROM
(
    SELECT
        update_time, id, deleted, type, `by`, time, text, dead, parent, poll,
        kids, url, score, title, parts, descendants
    FROM hackernews_history
    WHERE `by` = {username:String}
      AND toString(type) IN ('story', 'comment')
    ORDER BY id, update_time DESC
    LIMIT 1 BY id
)
ORDER BY time DESC
LIMIT {limit:UInt32}
FORMAT JSONEachRow
"""


UPSERT_HN_ITEM_SQL = """
INSERT INTO hn_user_items (
    id, author, item_type, hn_time, update_time, deleted, dead, parent, poll,
    kids, url, score, title, text, parts, descendants, raw
) VALUES (
    %(id)s, %(author)s, %(item_type)s, %(hn_time)s, %(update_time)s,
    %(deleted)s, %(dead)s, %(parent)s, %(poll)s, %(kids)s, %(url)s, %(score)s,
    %(title)s, %(text)s, %(parts)s, %(descendants)s, %(raw)s
)
ON CONFLICT (id) DO UPDATE SET
    author      = EXCLUDED.author,
    item_type   = EXCLUDED.item_type,
    hn_time     = EXCLUDED.hn_time,
    update_time = EXCLUDED.update_time,
    deleted     = EXCLUDED.deleted,
    dead        = EXCLUDED.dead,
    parent      = EXCLUDED.parent,
    poll        = EXCLUDED.poll,
    kids        = EXCLUDED.kids,
    url         = EXCLUDED.url,
    score       = EXCLUDED.score,
    title       = EXCLUDED.title,
    text        = EXCLUDED.text,
    parts       = EXCLUDED.parts,
    descendants = EXCLUDED.descendants,
    raw         = EXCLUDED.raw,
    imported_at = NOW()
"""


def _configured_usernames() -> list[str]:
    raw = os.environ.get("HN_USERNAMES", "user")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _configured_fetch_limit() -> int:
    return int(os.environ.get("HN_FETCH_LIMIT", "1000"))


def _configured_clickhouse_timeout() -> int:
    return int(os.environ.get("HN_CLICKHOUSE_TIMEOUT_SECONDS", "21600"))


def _clickhouse_bin() -> str:
    return os.environ.get("CLICKHOUSE_BIN", "clickhouse")


def _parse_clickhouse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # ClickHouse DateTime values are UTC seconds rendered without an offset in
    # JSONEachRow, e.g. "2026-06-20 00:53:24".
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=timezone.utc)


def _run_clickhouse_query(username: str, limit: int) -> list[dict[str, Any]]:
    sql = f"{CREATE_HN_TABLE_SQL};\n{SELECT_USER_ITEMS_SQL}"
    proc = subprocess.run(
        [
            _clickhouse_bin(),
            "local",
            "--multiquery",
            f"--param_username={username}",
            f"--param_limit={limit}",
            "--query",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=_configured_clickhouse_timeout(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"clickhouse local exited {proc.returncode}: {proc.stderr.strip()[-4000:]}"
        )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _decode_html_entities(value: Any) -> Any:
    if isinstance(value, str):
        return unescape(value)
    if isinstance(value, list):
        return [_decode_html_entities(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_html_entities(item) for key, item in value.items()}
    return value


def _row_for_upsert(row: dict[str, Any]) -> dict[str, Any]:
    row = _decode_html_entities(row)
    return {
        "id": int(row["id"]),
        "author": row.get("by") or "",
        "item_type": row.get("type") or "",
        "hn_time": _parse_clickhouse_datetime(row.get("time")),
        "update_time": _parse_clickhouse_datetime(row.get("update_time")),
        "deleted": bool(row.get("deleted", 0)),
        "dead": bool(row.get("dead", 0)),
        "parent": int(row["parent"]) if row.get("parent") else None,
        "poll": int(row["poll"]) if row.get("poll") else None,
        "kids": [int(v) for v in row.get("kids", [])],
        "url": row.get("url") or None,
        "score": int(row["score"]) if row.get("score") is not None else None,
        "title": row.get("title") or None,
        "text": row.get("text") or None,
        "parts": [int(v) for v in row.get("parts", [])],
        "descendants": (
            int(row["descendants"]) if row.get("descendants") is not None else None
        ),
        "raw": Jsonb(row),
    }


@op(retry_policy=RetryPolicy(max_retries=2, delay=120))
def import_hackernews_user_items_op(context) -> dict[str, int]:
    settings = load_settings()
    ensure_schema(settings.database_url)

    usernames = _configured_usernames()
    fetch_limit = _configured_fetch_limit()
    if not usernames:
        raise RuntimeError("HN_USERNAMES did not contain any usernames")

    total_read = 0
    total_upserted = 0
    by_user: dict[str, int] = {}

    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            for username in usernames:
                context.log.info(
                    "querying ClickHouse Hacker News dataset: user=%s limit=%d",
                    username,
                    fetch_limit,
                )
                rows = _run_clickhouse_query(username, fetch_limit)
                upsert_rows = [_row_for_upsert(row) for row in rows]
                total_read += len(rows)
                by_user[username] = len(rows)
                if upsert_rows:
                    cur.executemany(UPSERT_HN_ITEM_SQL, upsert_rows)
                    total_upserted += len(upsert_rows)
        conn.commit()

    context.log.info("Hacker News import rows by user: %s", by_user)
    return {"read": total_read, "upserted": total_upserted, **by_user}


@job
def hackernews_user_items_import_job():
    import_hackernews_user_items_op()


@schedule(
    cron_schedule="30 4 * * *",
    job=hackernews_user_items_import_job,
    default_status=DefaultScheduleStatus.RUNNING,
)
def hackernews_user_items_import_schedule(context):
    return RunRequest(
        run_key=f"hackernews-user-items-{context.scheduled_execution_time:%Y%m%d}"
    )
