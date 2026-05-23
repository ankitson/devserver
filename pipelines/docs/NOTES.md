# Open Threads / Gotchas

## Production status

- **Dagster pipeline** is the active one. `garmin_daily_schedule` is RUNNING
  (verified via GraphQL — note the CLI `dagster schedule list` shows the
  *code-defined default* which is `STOPPED`; live state lives in DAGSTER_HOME
  and is what the daemon and Dagit actually use).
- **DBOS** and **Restate** pipelines still build and run fine but their
  containers are dormant. Bring them back with `just up-dbos` / `just up-restate`
  any time. They share the mock bank and write to their own Postgres DBs
  (`pipeline_dbos`, `pipeline_restate`) — independent of each other and of
  `pipeline_dagster`.
- The original `garmin-fetch` cron container on the homeserver is still
  running. **Don't tear it down until the Dagster daily schedule has cleanly
  produced data for a few days in a row.** Both are idempotent and write to
  different DBs (`garmin` vs `pipeline_dagster`), so dual-run is safe.

## Garmin API quirks worth keeping in mind

- **`training_readiness`** returns `{}` for every day of this account.
  138/138 days, no exceptions. Either the user doesn't have it enabled, the
  watch doesn't support it, or it's account-region-locked. Not a pipeline
  bug — the parser correctly emits `no_data` and the row count stays 0.
  Decide later if you want to drop the asset entirely.
- **`hrv` / `stress`** have a few real data gaps even after live fetch — Garmin
  itself doesn't have data for those days (watch wasn't worn / didn't sync).
  133/138 days have full coverage; the missing 5 are genuine zero-data days.
- **`heart_rate_samples`** uses DELETE+INSERT not upsert — re-running an
  already-materialised partition will replace its samples wholesale. Idempotent
  from a row-content perspective.

## Auth / rate limits

- **Garth is deprecated upstream.** The mobile-auth strategy `garth` provides
  is the first one `Garmin.login()` tries; it 429s reliably for us now. The
  library cascades through `mobile+requests` → `widget+cffi` → `portal+cffi`
  and one of those usually works. The OAuth refresh tokens any successful
  strategy writes are still standard JWTs and live in
  `/root/.garth/garmin_tokens.json` (persistent volume `dagster-garth`). They
  refresh transparently for ~1 year. If Garmin changes the refresh endpoint
  the whole thing collapses — fork garth or vendor a replacement if/when that
  happens.
- **First login after a token wipe is slow** (the 2026-04-10 partition took
  166 s because of the 429 cascade). Every login afterward loads tokens
  from disk and skips the cascade entirely (~0 s extra). If you ever
  `docker volume rm dagster-garth` you'll pay that bootstrap cost again.
- **In-process rate limiter** is set to `GARMIN_MIN_REQUEST_INTERVAL_SECONDS=2.5`
  — well above garmin-fetch's own 1 s floor. This is what makes each
  cache-miss partition ~18 s (7 API calls × 2.5 s). Lower the floor if you
  want it faster — but back-off was the source of all our 429 grief, so be
  cautious.
- **Run-tag concurrency limit on `garmin_api` is 1** in `dagster.yaml`. Means
  every Garmin-touching backfill or schedule run serialises. Bank workflows
  and notifier-drain runs aren't tagged so they don't compete.

## Things that look broken but aren't

- **`dagster schedule list` CLI says STOPPED, the UI says RUNNING.** Trust the
  UI — that's the live daemon state. CLI shows the source-code default
  (`default_status=DefaultScheduleStatus.STOPPED`).
- **`run_queue.max_concurrent_runs` validation error** kept appearing during
  the dagster.yaml work — Dagster's own default `QueuedRunCoordinator`
  instance carries an implicit `max_concurrent_runs` that conflicts with
  the new-style `concurrency.runs.*` block. The resolution was to use the
  explicit `run_coordinator: { module, class, config }` form (see dagster.yaml).
- **Partition `2026-04-10` looks "rate-limited"** in run logs because of
  the `body_battery STEP_UP_FOR_RETRY` line — but all data did land. The
  retry happened automatically and the partition is materialised cleanly.
  See the discussion at the end of 2026-05-18 in chat log if you need to
  re-explain this to yourself.

## Stuff we haven't done yet

- **X / Twitter bookmarks** — the custom pipeline + viewer were removed
  (archived at git tag `x-bookmarks-archive`). The plan is to adopt
  [birdclaw](https://github.com/steipete/birdclaw) (CLI + webapp over its own
  SQLite, using `xurl` for auth) instead of a hand-rolled fetcher. Not yet set up.

- **ntfy push** — code is in place, but `NTFY_TOPIC` is empty in all
  secrets templates so anomaly notifications stay in the `notifications`
  table only. Set the topic + (optional) token in
  `secrets/pipeline-dagster.env` to enable phone push.

- **`activities` cache** is independent of the 7 metrics and uses a
  read-only fall-through to source `garmin.raw_responses`. If you want
  the dagster pipeline to fetch fresh activities, add an asset for it
  (currently `_reparse_hr_samples` consumes whatever activity windows
  it can find in cache).

- **Validation asset checks** only ported the simple NULL / zero checks
  from `validate.py`. No cross-metric checks ("if sleep exists then
  heart_rate should too") and no anomaly threshold checks at the asset
  level. The anomaly_candidates asset handles the latter outside of the
  asset-check system.

- **Cancelled backfills** (`xzalikec`, `xmzqobwi`) and the first attempt
  (`akephqmf`, `jqrvwqna`) are still visible in Dagit history. Harmless,
  but they make the backfill list noisy. If it bugs you: `dagster
  backfill wipe ...` or delete from `dagster.bulk_actions` table.

## Things to think about next

- **DAGSTER_HOME storage is SQLite.** For a personal project this is fine.
  If run history starts to bloat, switch storage to Postgres (
  `pipelines/dagster/dagster.yaml` → `run_storage`, `event_log_storage`,
  `schedule_storage` blocks → point them at the shared instance). Token
  cache + garmin volumes stay on local FS.
- **Adding new sources** is meant to be small: write a fetcher in
  `pipeline_shared`, expose it as a daily-partitioned asset under
  `pipelines/dagster/src/pipeline_dagster_proj/assets.py`. The retry
  policy, concurrency tag, in_process executor, and token-cache
  machinery all apply automatically if you use the existing
  `GarminPipelineResource` or write a sibling resource.
- **Reducing the 18 s per cache-miss partition** would need either a
  lower rate-limit floor (risky) or batching multiple metric requests
  per HTTP call (Garmin doesn't expose a batch endpoint, AFAICT).
- **Calendar source** was in the original plan but never built. Pipeline
  shape would mirror Garmin: daily-partitioned, idempotent upserts.
  Keeper already syncs calendar into its own Postgres so the "source"
  could be Keeper's DB rather than a new API client.
