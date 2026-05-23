# x-bookmarks

A faithful, read-only web view of your X (Twitter) bookmarks.

Served at **https://bookmarks.home.ankitson.com** via the homeserver
[app-runner](../../homeserver/docs/app-runner.md) tier (port 9005).

## What it shows

Renders each bookmark as an X-style card: author name + @handle, timestamp,
rich text (URLs / @mentions / #hashtags linkified), image & video media,
nested **quoted tweets**, expandable **threads**, and public metrics
(reply / retweet / like / bookmark / views). Cards are ordered by bookmark
recency (`bookmarked_at` desc, then `sort_index`).

## Data source

Reads, read-only, from the `pipeline_dbos` Postgres database that the DBOS
X-bookmarks pipeline writes to (`devserver/pipelines/shared/.../x_bookmarks.py`):

| Table | Used for |
|---|---|
| `x_bookmarks` | the bookmark list + order (`sort_index`, `bookmarked_at`) |
| `x_tweets` | tweet text, kind, refs, `payload` (created_at, public_metrics) |
| `x_users` | author name / @handle / avatar |
| `x_media` | image & video URLs |

No writes. Pure projection of pipeline state — refresh the page to see new
bookmarks after the pipeline's hourly fetch.

## Notes

- **Avatars** depend on `x_users.profile_image_url`, which only populates on
  fetches made after the pipeline started requesting `user.fields`. Authors
  without one get a deterministic colored initial.
- There is **no real "bookmarked at" timestamp** from X's API — `bookmarked_at`
  is a first-seen proxy and `sort_index` preserves list order at first sighting.

## Routes

- `GET /` — the rendered timeline (server-side HTML)
- `GET /api/bookmarks` — assembled bookmarks as JSON
- `GET /api/health` — `{ ok: true }`

## Local dev

The DB host `postgres` only resolves on the `mybridge` Docker network, so this
runs inside the app-runner container, not on the host. Config in
[`apps.json`](../../homeserver/config/app-runner/apps.json); secret in `.env`
(rendered from `.env.tmpl` via `op inject`).
