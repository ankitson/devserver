"""X (Twitter) bookmarks ingestion.

API surface used: X API v2 with OAuth 2.0 user-context tokens.

  GET /2/users/me                          — resolve the authenticated user
  GET /2/users/:id/bookmarks               — list bookmarks (paginated)
  GET /2/tweets                            — batch lookup tweets by id
  GET /2/tweets/search/recent              — used to gather thread replies

Storage (added to schema.py — see init_schema):
  x_oauth_tokens(account, access_token, refresh_token, expires_at, ...)
  x_bookmarks_state(account, last_cursor, last_fetched_at)
  x_tweets(id, author_id, conversation_id, text, kind, referenced_tweet_id,
           media_keys jsonb, payload jsonb, fetched_at)
  x_media(media_key, type, url, preview_image_url, local_path, fetched_at)
  x_bookmarks(account, tweet_id, bookmarked_at) — many-to-one to x_tweets

Pipeline shape (intentionally idempotent + cache-first):

  1. ensure_x_schema()                      — adds the above tables
  2. fetch_bookmarks_page(client, cursor)   — pull a page, write raw to
                                                raw_responses, return parsed list
  3. resolve_referenced(client, tweet)      — for quote/reply, fetch the
                                                referenced tweet if not cached
  4. resolve_thread(client, conversation_id) — if root author == bookmark author,
                                                fetch full conversation
  5. record_media(tweet)                     — index media; download is optional

Conservative rate limiting:
  - X user-context bookmarks endpoint = 1 request / 15 minutes (yes, really).
  - We sleep at least 16 minutes between fetch_bookmarks_page calls.
  - Other endpoints (tweets lookup, search recent) are 300 / 15 minutes —
    well above the volume we generate per run.

Auth flow (PKCE, manual):
  - `print_authorize_url()` prints the URL the user opens in a browser.
  - User authorizes, gets redirected to redirect_uri with `?code=`.
  - `exchange_code_for_tokens(code, verifier)` swaps it for access + refresh.
  - `refresh_tokens()` is called transparently before each request when the
    stored access_token is expiring within 60s.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
import psycopg

from pipeline_shared.config import Settings

log = logging.getLogger(__name__)

X_API = "https://api.x.com/2"
X_OAUTH_AUTHZ = "https://x.com/i/oauth2/authorize"
X_OAUTH_TOKEN = "https://api.x.com/2/oauth2/token"
DEFAULT_SCOPES = ["tweet.read", "users.read", "bookmark.read", "offline.access"]
DEFAULT_BOOKMARK_RATE_LIMIT_SECONDS = 16 * 60  # 16 minutes ≈ 1 / 15min


X_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS x_oauth_tokens (
    account        TEXT PRIMARY KEY,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT,
    expires_at     TIMESTAMPTZ NOT NULL,
    scopes         TEXT[],
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS x_bookmarks_state (
    account            TEXT PRIMARY KEY,
    next_cursor        TEXT,
    last_fetched_at    TIMESTAMPTZ,
    last_completed_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS x_tweets (
    id                   TEXT PRIMARY KEY,
    author_id            TEXT,
    conversation_id      TEXT,
    text                 TEXT,
    kind                 TEXT NOT NULL DEFAULT 'bookmark',
    referenced_tweet_id  TEXT,
    referenced_kind      TEXT,
    media_keys           JSONB,
    payload              JSONB,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS x_tweets_conv ON x_tweets (conversation_id);

CREATE TABLE IF NOT EXISTS x_media (
    media_key          TEXT PRIMARY KEY,
    type               TEXT,
    url                TEXT,
    preview_image_url  TEXT,
    local_path         TEXT,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS x_bookmarks (
    account        TEXT NOT NULL,
    tweet_id       TEXT NOT NULL REFERENCES x_tweets(id) ON DELETE CASCADE,
    bookmarked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account, tweet_id)
);
"""


def ensure_x_schema(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(X_SCHEMA_SQL)


# ── rate limiter (singleton, process-wide) ──────────────────────────────

_BOOKMARK_LIMITER_LOCK = threading.Lock()
_BOOKMARK_LAST: dict[str, float] = {}


def _wait_for_bookmark_rate_limit(account: str, min_interval: float) -> None:
    with _BOOKMARK_LIMITER_LOCK:
        last = _BOOKMARK_LAST.get(account, 0.0)
        wait_s = (last + min_interval) - time.monotonic()
        if wait_s > 0:
            log.info("[x] sleeping %.0fs for bookmark rate limit", wait_s)
            time.sleep(wait_s)
        _BOOKMARK_LAST[account] = time.monotonic()


# ── OAuth helpers ───────────────────────────────────────────────────────


@dataclass
class XClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    account: str = "default"  # logical name for the stored token


def make_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge_S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg: XClientConfig, verifier: str, state: str | None = None,
                  scopes: list[str] = DEFAULT_SCOPES) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(scopes),
        "state": state or secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode
    return f"{X_OAUTH_AUTHZ}?{urlencode(params)}"


def exchange_code_for_tokens(
    cfg: XClientConfig, code: str, code_verifier: str,
) -> dict:
    """Exchange the authorization code for access + refresh tokens."""
    auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
    data = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": code_verifier,
    }
    r = httpx.post(X_OAUTH_TOKEN, data=data, auth=auth, timeout=15.0)
    r.raise_for_status()
    return r.json()


def refresh_tokens(cfg: XClientConfig, refresh_token: str) -> dict:
    auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
    data = {
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "client_id": cfg.client_id,
    }
    r = httpx.post(X_OAUTH_TOKEN, data=data, auth=auth, timeout=15.0)
    r.raise_for_status()
    return r.json()


def save_tokens(database_url: str, account: str, token_response: dict) -> None:
    expires_in = int(token_response.get("expires_in", 7200))
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    scopes = (token_response.get("scope") or "").split() or None
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO x_oauth_tokens
                (account, access_token, refresh_token, expires_at, scopes)
            VALUES (%s, %s, %s, to_timestamp(%s), %s)
            ON CONFLICT (account) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = COALESCE(EXCLUDED.refresh_token,
                                          x_oauth_tokens.refresh_token),
                expires_at = EXCLUDED.expires_at,
                scopes = COALESCE(EXCLUDED.scopes, x_oauth_tokens.scopes),
                updated_at = NOW()
            """,
            (account, token_response["access_token"],
             token_response.get("refresh_token"), expires_at, scopes),
        )


def load_tokens(database_url: str, account: str) -> dict | None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT access_token, refresh_token,
                       EXTRACT(EPOCH FROM expires_at)::bigint AS exp
                  FROM x_oauth_tokens WHERE account = %s
                """,
                (account,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


# ── X client ────────────────────────────────────────────────────────────


class XClient:
    """Tiny wrapper around httpx that handles token refresh and rate limiting."""

    def __init__(self, settings: Settings, cfg: XClientConfig):
        self.settings = settings
        self.cfg = cfg
        self._token_cache: dict | None = None

    def _ensure_token(self) -> str:
        cache = self._token_cache or load_tokens(self.settings.database_url, self.cfg.account)
        if cache is None:
            raise RuntimeError(
                f"No saved X OAuth tokens for account '{self.cfg.account}'. "
                "Run the authorize flow first."
            )
        now = int(time.time())
        if cache["expires_at"] <= now + 60 and cache.get("refresh_token"):
            log.info("[x] refreshing access_token for %s", self.cfg.account)
            tr = refresh_tokens(self.cfg, cache["refresh_token"])
            save_tokens(self.settings.database_url, self.cfg.account, tr)
            cache = load_tokens(self.settings.database_url, self.cfg.account)
        self._token_cache = cache
        return cache["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}",
                "User-Agent": "pipeline-shared/x-bookmarks"}

    def me(self) -> dict:
        r = httpx.get(f"{X_API}/users/me", headers=self._headers(), timeout=15.0)
        r.raise_for_status()
        return r.json()["data"]

    def list_bookmarks(self, user_id: str, *, pagination_token: str | None = None,
                        max_results: int = 50) -> dict:
        """One page of bookmarks. Caller is responsible for spacing requests
        — the bookmarks endpoint is rate-limited to 1/15min user-context."""
        params = {
            "max_results": str(max_results),
            "tweet.fields": "id,author_id,conversation_id,created_at,text,"
                             "referenced_tweets,attachments,public_metrics",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "media.fields": "media_key,type,url,preview_image_url,variants",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        r = httpx.get(
            f"{X_API}/users/{user_id}/bookmarks",
            headers=self._headers(), params=params, timeout=30.0,
        )
        r.raise_for_status()
        return r.json()

    def lookup_tweets(self, ids: list[str]) -> dict:
        params = {
            "ids": ",".join(ids),
            "tweet.fields": "id,author_id,conversation_id,created_at,text,"
                             "referenced_tweets,attachments",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "media.fields": "media_key,type,url,preview_image_url,variants",
        }
        r = httpx.get(f"{X_API}/tweets", headers=self._headers(), params=params,
                      timeout=30.0)
        r.raise_for_status()
        return r.json()

    def search_thread(self, conversation_id: str, author_id: str) -> list[dict]:
        """Find all tweets in a conversation by a given author — i.e. the thread.

        Uses /tweets/search/recent. Limited to the last 7 days for free tier,
        which is fine for active bookmarks but loses old threads.
        """
        query = f"conversation_id:{conversation_id} from:{author_id}"
        params = {
            "query": query,
            "max_results": "100",
            "tweet.fields": "id,author_id,conversation_id,created_at,text,"
                             "referenced_tweets,attachments",
            "expansions": "attachments.media_keys",
            "media.fields": "media_key,type,url,preview_image_url,variants",
        }
        r = httpx.get(f"{X_API}/tweets/search/recent", headers=self._headers(),
                      params=params, timeout=30.0)
        if r.status_code == 200:
            data = r.json()
            return data.get("data", []) or []
        log.warning("[x] thread search failed (%s): %s", r.status_code, r.text[:200])
        return []


# ── storage helpers ─────────────────────────────────────────────────────


def _upsert_tweet(cur, t: dict, kind: str = "bookmark",
                   referenced: dict | None = None) -> None:
    refs = t.get("referenced_tweets") or []
    if not referenced and refs:
        referenced = {"id": refs[0]["id"], "type": refs[0]["type"]}
    media_keys = (t.get("attachments") or {}).get("media_keys") or []
    cur.execute(
        """
        INSERT INTO x_tweets
            (id, author_id, conversation_id, text, kind, referenced_tweet_id,
             referenced_kind, media_keys, payload, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
        ON CONFLICT (id) DO UPDATE SET
            author_id = EXCLUDED.author_id,
            conversation_id = EXCLUDED.conversation_id,
            text = EXCLUDED.text,
            kind = EXCLUDED.kind,
            referenced_tweet_id = EXCLUDED.referenced_tweet_id,
            referenced_kind = EXCLUDED.referenced_kind,
            media_keys = EXCLUDED.media_keys,
            payload = EXCLUDED.payload,
            fetched_at = NOW()
        """,
        (
            t["id"], t.get("author_id"), t.get("conversation_id"),
            t.get("text"), kind,
            referenced["id"] if referenced else None,
            referenced["type"] if referenced else None,
            json.dumps(media_keys),
            json.dumps(t),
        ),
    )


def _upsert_media_rows(cur, includes: dict) -> None:
    for m in (includes.get("media") or []):
        cur.execute(
            """
            INSERT INTO x_media (media_key, type, url, preview_image_url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (media_key) DO UPDATE SET
                type = EXCLUDED.type,
                url = COALESCE(EXCLUDED.url, x_media.url),
                preview_image_url = COALESCE(EXCLUDED.preview_image_url,
                                              x_media.preview_image_url),
                fetched_at = NOW()
            """,
            (m["media_key"], m.get("type"),
             m.get("url") or _best_variant_url(m),
             m.get("preview_image_url")),
        )


def _best_variant_url(media: dict) -> str | None:
    variants = (media.get("variants") or [])
    if not variants:
        return None
    mp4 = [v for v in variants if v.get("content_type") == "video/mp4"]
    if mp4:
        mp4.sort(key=lambda v: v.get("bit_rate", 0), reverse=True)
        return mp4[0].get("url")
    return variants[0].get("url")


# ── main pipeline operation ─────────────────────────────────────────────


def fetch_and_store_bookmarks(
    *,
    settings: Settings,
    cfg: XClientConfig,
    pages: int = 1,
    rate_limit_seconds: float = DEFAULT_BOOKMARK_RATE_LIMIT_SECONDS,
    resolve_threads: bool = True,
    resolve_quotes: bool = True,
) -> dict:
    """End-to-end one-run fetch.

    Pulls up to `pages` pages from the bookmarks list (one page per ~15min
    due to rate limit), upserts each tweet, resolves quotes + threads.
    Returns counters: {pages, bookmarks_seen, new_tweets, threads_pulled,
                       quotes_pulled, media_indexed}.
    """
    ensure_x_schema(settings.database_url)
    client = XClient(settings, cfg)
    me = client.me()
    user_id = me["id"]

    state = _load_state(settings.database_url, cfg.account)
    cursor = state.get("next_cursor")
    counters = {"pages": 0, "bookmarks_seen": 0, "new_tweets": 0,
                "threads_pulled": 0, "quotes_pulled": 0, "media_indexed": 0}

    for _ in range(pages):
        _wait_for_bookmark_rate_limit(cfg.account, rate_limit_seconds)
        try:
            page = client.list_bookmarks(user_id, pagination_token=cursor)
        except httpx.HTTPStatusError as e:
            log.error("[x] bookmarks request failed: %s — %s", e, e.response.text[:200])
            raise
        _store_raw_response(settings.database_url, cfg.account, page,
                            kind=f"bookmarks_page_{counters['pages']}")
        counters["pages"] += 1
        tweets = page.get("data") or []
        counters["bookmarks_seen"] += len(tweets)
        includes = page.get("includes") or {}

        with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
            _upsert_media_rows(cur, includes)
            counters["media_indexed"] += len(includes.get("media") or [])
            for t in tweets:
                cur.execute("SELECT 1 FROM x_tweets WHERE id = %s", (t["id"],))
                is_new = cur.fetchone() is None
                _upsert_tweet(cur, t, kind="bookmark")
                cur.execute(
                    """
                    INSERT INTO x_bookmarks (account, tweet_id, bookmarked_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    (cfg.account, t["id"]),
                )
                if is_new:
                    counters["new_tweets"] += 1
            conn.commit()

            if resolve_quotes:
                quote_ids = []
                for t in tweets:
                    for ref in (t.get("referenced_tweets") or []):
                        if ref.get("type") == "quoted":
                            quote_ids.append(ref["id"])
                for chunk in _chunks(quote_ids, 100):
                    rsp = client.lookup_tweets(chunk)
                    _store_raw_response(settings.database_url, cfg.account, rsp,
                                        kind="quote_lookup")
                    qincludes = rsp.get("includes") or {}
                    _upsert_media_rows(cur, qincludes)
                    for q in (rsp.get("data") or []):
                        _upsert_tweet(cur, q, kind="quoted")
                        counters["quotes_pulled"] += 1
                    conn.commit()

            if resolve_threads:
                threads_to_fetch = []
                for t in tweets:
                    if t.get("conversation_id") and t["conversation_id"] != t["id"] \
                            and t.get("author_id"):
                        threads_to_fetch.append(
                            (t["conversation_id"], t["author_id"])
                        )
                seen = set()
                for conv_id, author_id in threads_to_fetch:
                    if (conv_id, author_id) in seen:
                        continue
                    seen.add((conv_id, author_id))
                    reps = client.search_thread(conv_id, author_id)
                    if not reps:
                        continue
                    counters["threads_pulled"] += 1
                    for r in reps:
                        _upsert_tweet(cur, r, kind="thread_reply")
                    conn.commit()

        meta = page.get("meta") or {}
        cursor = meta.get("next_token")
        _save_state(settings.database_url, cfg.account, cursor)
        if not cursor:
            break

    _save_state(settings.database_url, cfg.account, cursor, completed=True)
    return counters


def _store_raw_response(database_url: str, account: str, payload: dict,
                        *, kind: str) -> None:
    """Cache the raw X API JSON in `raw_responses` so we can re-derive without
    re-hitting the API. We pretend each response is for the (date=today, metric=
    'x_<account>_<kind>') slot — sidestepping a separate raw table while keeping
    the existing schema model.
    """
    from datetime import date as _date
    metric = f"x_{account}_{kind}"
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_responses (date, metric, response, fetched_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (date, metric) DO UPDATE SET
                response = EXCLUDED.response,
                fetched_at = NOW()
            """,
            (_date.today().isoformat(), metric, json.dumps(payload)),
        )


def _load_state(database_url: str, account: str) -> dict:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT next_cursor FROM x_bookmarks_state WHERE account=%s",
            (account,),
        )
        row = cur.fetchone()
    return {"next_cursor": row[0]} if row else {}


def _save_state(database_url: str, account: str, next_cursor: str | None,
                completed: bool = False) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO x_bookmarks_state
                (account, next_cursor, last_fetched_at, last_completed_at)
            VALUES (%s, %s, NOW(), CASE WHEN %s THEN NOW() ELSE NULL END)
            ON CONFLICT (account) DO UPDATE SET
                next_cursor = EXCLUDED.next_cursor,
                last_fetched_at = NOW(),
                last_completed_at = CASE WHEN %s
                                         THEN NOW()
                                         ELSE x_bookmarks_state.last_completed_at
                                    END
            """,
            (account, next_cursor, completed, completed),
        )


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
