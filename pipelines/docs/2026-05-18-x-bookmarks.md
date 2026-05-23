# X/Twitter Bookmarks Pipeline

A new data source added to `pipeline_shared`. Currently wired into the DBOS
pipeline (the same shape can be ported to Dagster/Restate later — the
library code is orchestrator-agnostic).

## What it does

1. Pulls your X bookmarks via the user-context OAuth 2.0 API.
2. Stores every raw API response in `raw_responses` (timestamped, idempotent),
   so re-derive never needs to re-call the API.
3. Upserts tweets into `x_tweets`, media into `x_media`, bookmark links into
   `x_bookmarks`.
4. **Quote tweets**: for any bookmark with `referenced_tweets.type='quoted'`,
   it fetches the quoted tweet (`/2/tweets?ids=…`) and stores it with
   `kind='quoted'` + `referenced_tweet_id` pointing back.
5. **Threads**: for bookmarks where `conversation_id != id`, it searches the
   conversation (`/2/tweets/search/recent`) for further tweets by the original
   author and stores them with `kind='thread_reply'`.
6. **Media**: image URLs, video URLs (highest-bitrate mp4 variant) and
   preview images are indexed in `x_media`. Local download is intentionally
   not implemented yet — add a downloader step that resolves `local_path`
   when needed.

## Tables (created on first run)

```
x_oauth_tokens     (account, access_token, refresh_token, expires_at, scopes)
x_bookmarks_state  (account, next_cursor, last_fetched_at, last_completed_at)
x_tweets           (id, author_id, conversation_id, text, kind,
                    referenced_tweet_id, referenced_kind, media_keys, payload)
x_media            (media_key, type, url, preview_image_url, local_path)
x_bookmarks        (account, tweet_id, bookmarked_at)
```

`raw_responses` also accumulates rows with metric=`x_<account>_<kind>` —
mirrors the same cache-first contract we use for Garmin.

## Conservative rate limiting

The X user-context **bookmarks** endpoint has a brutally low limit
(1 request / 15 minutes). The library enforces a process-wide gate at
`X_BOOKMARK_RATE_LIMIT_SECONDS` (default 960s = 16 min) so multiple
concurrent calls cannot trip it. The DBOS workflow's per-step retry policy
is 60s × up to 3 attempts — meant for transient 5xx, not for rate-limit
recovery (which would burn quota).

Other endpoints (tweet lookup, recent-search) are 300 / 15 min — well
above the volume we generate.

## Auth (one-time per account)

OAuth 2.0 with PKCE, manual code flow. From host (or any pipeline
container):

```bash
just pipeline-x-init dbos
# prints:
#   1. URL → open in browser, authorize
#   2. you'll be redirected to /x-callback?code=…&state=…
#   3. copy the code, then:
just pipeline-x-exchange dbos <code> <verifier-from-step-1>
```

Tokens are saved to `x_oauth_tokens`. The client transparently refreshes
when the access_token is within 60s of expiry.

## On-demand fetch

```bash
# fetch one bookmark page (≈ 1 wait of 16 minutes if not the first call)
just pipeline-x-fetch dbos 1

# or via HTTP
curl -X POST http://localhost:18801/trigger/x_bookmarks -d 'pages=1'
```

Both return a counters dict:

```
{"pages":1, "bookmarks_seen":N, "new_tweets":N, "threads_pulled":N,
 "quotes_pulled":N, "media_indexed":N}
```

## Scheduled fetch (DBOS)

`@DBOS.scheduled("0 * * * *")` calls `fetch_x_bookmarks_step(pages=1)`
hourly. With the 16-min internal gate, an hourly schedule means at most
1 page per hour, which matches the API limit and gives a 15-minute
cushion for retries. If no tokens are stored yet, the tick logs
"skipped" and exits — no error.

## Backfill

The first page returns the most recent bookmarks. To go further back,
call with `pages>1`. Each page increment costs another 16-minute wait.
The cursor lives in `x_bookmarks_state.next_cursor` and survives
container restarts, so a backfill can be resumed across days.

## Secrets

Template at `config/pipeline-dbos.env.tmpl`:

```
X_OAUTH_CLIENT_ID={{ op://clankers/x/oauth-2-clientid }}
X_OAUTH_CLIENT_SECRET={{ op://clankers/x/oauth-2-clientsecret }}
X_OAUTH_REDIRECT_URI=http://localhost:18801/x-callback
X_ACCOUNT=default
X_BOOKMARK_RATE_LIMIT_SECONDS=960
```

Render via `just rs` (uses `op inject`).

## Files

- `pipelines/shared/src/pipeline_shared/x_bookmarks.py` — full library.
- `pipelines/dbos/src/pipeline_dbos/workflows.py` — `fetch_x_bookmarks_workflow`,
  `fetch_x_bookmarks_step`, `x_bookmarks_hourly` scheduled.
- `pipelines/dbos/src/pipeline_dbos/main.py` — `/x-callback`, `/trigger/x_bookmarks`,
  `/x-status`.
- Justfile: `pipeline-x-init`, `pipeline-x-exchange`, `pipeline-x-fetch`.

## Known limitations

- Recent-search threading only sees the last 7 days of conversation by
  default (Twitter free-tier API restriction). Older bookmarked threads
  won't get fully expanded.
- Media is indexed, not downloaded. Add a step that walks `x_media`
  rows with `local_path IS NULL` and downloads to a volume.
- Quote-of-quote not chased recursively. If the bookmark is a quote of a
  quote, only the first-level quoted tweet is pulled.
- Dagster + Restate wrappers not yet added — only DBOS has the workflow.
  Same primitives in `pipeline_shared.x_bookmarks` can be called from a
  Dagster asset / Restate handler in ~30 lines each.
