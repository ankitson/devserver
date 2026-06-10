"""Mirror birdclaw bookmarks (SQLite, in app-runner's volume) into the
pipeline postgres.

birdclaw's bookmark sync (separate `birdclaw_sync_bookmarks_job`) pulls live
from X into a local SQLite. This job reads that SQLite read-only and upserts
each bookmark row into `x_bookmarks` in postgres so downstream
queries / dashboards don't have to reach into another container's filesystem.

Idempotent: re-running upserts the latest values; primary key (account_id,
tweet_id) prevents duplicates.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg
from dagster import RetryPolicy, op, job

from pipeline_shared.config import load_settings
from pipeline_shared.schema import ensure_schema


# Bind-mounted from the homeserver compose. The importer opens the DB with
# mode=ro, but the mount itself must be writable so SQLite can create/use WAL
# sidecar coordination files (`birdclaw.sqlite-wal` / `birdclaw.sqlite-shm`).
BIRDCLAW_SQLITE_PATH = os.environ.get(
    "BIRDCLAW_SQLITE_PATH", "/birdclaw/birdclaw.sqlite"
)

# Mirrors birdclaw's bookmarkedOnly query: tweets with bookmarked=1 (archive
# legacy) OR a tweet_collections row of kind='bookmarks' (live sync).
SELECT_BOOKMARKS_SQL = """
SELECT t.id                                AS tweet_id,
       t.account_id                        AS account_id,
       p.handle                            AS author_handle,
       p.display_name                      AS author_display_name,
       t.text                              AS text,
       t.created_at                        AS tweet_created_at,
       t.like_count                        AS like_count,
       t.media_count                       AS media_count,
       COALESCE(c.collected_at, c.updated_at) AS bookmarked_at
FROM tweets t
JOIN profiles p ON p.id = t.author_profile_id
LEFT JOIN tweet_collections c
       ON c.tweet_id   = t.id
      AND c.account_id = t.account_id
      AND c.kind       = 'bookmarks'
WHERE t.bookmarked = 1 OR c.tweet_id IS NOT NULL
ORDER BY t.created_at DESC, t.id DESC
"""

UPSERT_SQL = """
INSERT INTO x_bookmarks (
    account_id, tweet_id, author_handle, author_display_name,
    text, tweet_created_at, like_count, media_count,
    bookmarked_at, tweet_url
) VALUES (
    %(account_id)s, %(tweet_id)s, %(author_handle)s, %(author_display_name)s,
    %(text)s, %(tweet_created_at)s, %(like_count)s, %(media_count)s,
    %(bookmarked_at)s, %(tweet_url)s
)
ON CONFLICT (account_id, tweet_id) DO UPDATE SET
    author_handle       = EXCLUDED.author_handle,
    author_display_name = EXCLUDED.author_display_name,
    text                = EXCLUDED.text,
    tweet_created_at    = EXCLUDED.tweet_created_at,
    like_count          = EXCLUDED.like_count,
    media_count         = EXCLUDED.media_count,
    bookmarked_at       = EXCLUDED.bookmarked_at,
    tweet_url           = EXCLUDED.tweet_url,
    imported_at         = NOW()
"""


def _tweet_url(handle: str | None, tweet_id: str) -> str:
    # Unknown / placeholder authors still resolve via the generic /i/status/
    # path, so the URL is always clickable.
    handle = (handle or "").strip()
    if not handle or handle == "unknown":
        return f"https://x.com/i/status/{tweet_id}"
    return f"https://x.com/{handle}/status/{tweet_id}"


def _normalize_timestamp(value: Any) -> str | None:
    # birdclaw stores ISO-8601 strings ("2024-03-04T05:06:07.000Z"); postgres
    # accepts them directly. None when the source row had no timestamp.
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _readonly_sqlite_uri(sqlite_path: str) -> str:
    path = Path(sqlite_path)
    if not path.is_absolute():
        path = path.resolve()
    return f"{path.as_uri()}?mode=ro"


def _rows_for_upsert(cursor: sqlite3.Cursor) -> Iterable[dict[str, Any]]:
    for row in cursor:
        yield {
            "account_id": row["account_id"],
            "tweet_id": row["tweet_id"],
            "author_handle": row["author_handle"] or "unknown",
            "author_display_name": row["author_display_name"],
            "text": row["text"] or "",
            "tweet_created_at": _normalize_timestamp(row["tweet_created_at"]),
            "like_count": int(row["like_count"] or 0),
            "media_count": int(row["media_count"] or 0),
            "bookmarked_at": _normalize_timestamp(row["bookmarked_at"]),
            "tweet_url": _tweet_url(row["author_handle"], row["tweet_id"]),
        }


@op(retry_policy=RetryPolicy(max_retries=2, delay=60))
def import_birdclaw_bookmarks_op(context) -> dict[str, int]:
    sqlite_path = BIRDCLAW_SQLITE_PATH
    if not os.path.exists(sqlite_path):
        raise RuntimeError(
            f"birdclaw sqlite not found at {sqlite_path} — is the volumes/birdclaw "
            "bind mount wired into pipeline-dagster?"
        )

    settings = load_settings()
    ensure_schema(settings.database_url)

    # Keep the SQLite connection itself read-only while allowing SQLite's
    # normal WAL read path to use the sidecar files on the writable mount.
    sqlite_uri = _readonly_sqlite_uri(sqlite_path)
    src = sqlite3.connect(sqlite_uri, uri=True, isolation_level=None)
    src.row_factory = sqlite3.Row
    try:
        # Count first so the run log shows scope before the upsert.
        total = src.execute(
            "SELECT count(*) FROM ("
            "  SELECT t.id FROM tweets t LEFT JOIN tweet_collections c"
            "    ON c.tweet_id=t.id AND c.account_id=t.account_id AND c.kind='bookmarks'"
            "  WHERE t.bookmarked=1 OR c.tweet_id IS NOT NULL"
            ")"
        ).fetchone()[0]
        context.log.info("birdclaw bookmarks visible in sqlite: %d", total)

        cursor = src.execute(SELECT_BOOKMARKS_SQL)
        rows = list(_rows_for_upsert(cursor))
    finally:
        src.close()

    if not rows:
        return {"read": 0, "upserted": 0}

    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, rows)
        conn.commit()

    by_account: dict[str, int] = {}
    for row in rows:
        by_account[row["account_id"]] = by_account.get(row["account_id"], 0) + 1
    context.log.info("upserted bookmarks by account: %s", by_account)

    return {"read": len(rows), "upserted": len(rows), **by_account}


@job
def birdclaw_bookmarks_import_job():
    import_birdclaw_bookmarks_op()
