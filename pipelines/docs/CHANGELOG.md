# Devserver Changelog

## 2026-05-23 — Removed custom X/Twitter bookmarks pipeline + viewer
Retired the hand-rolled X bookmarks stack (`pipeline_shared.x_bookmarks`, the
DBOS workflow/schedule/endpoints, the `x_*` Postgres tables, the `x-*` CLI
subcommands, and the standalone Bun viewer at `bookmarks.home.ankitson.com`).
Superseded by the plan to adopt [birdclaw](https://github.com/steipete/birdclaw)
(CLI + webapp over its own SQLite, auth via `xurl`). The full implementation is
preserved in git at tag **`x-bookmarks-archive`**.

## 2026-05-18 / 2026-05-19 — Data pipelines build-out

### Planning (2026-05-18)
- General data-pipeline plan: `2026-05-18-data-pipelines-plan.md` — tool-agnostic
  contract for Garmin (asset-graph ETL) + Calendar + Banking (durable workflow
  with human OTP) on the existing shared Postgres.
- Per-tool plans for Dagster, Restate, DBOS: `2026-05-18-pipeline-*.md`.

### Pipeline implementations built
All three pipelines were implemented, dockerised, and verified end-to-end
against the same shared Postgres + the same mock-bank service. See
`2026-05-18-pipelines-verification.md` for the cross-pipeline diff
(parsed data was bit-identical across all three).

- `pipelines/shared/` — domain library, wraps `/projects/garmin-fetch` as a
  dependency. Adds:
  - `pipeline_shared.garmin` — run-scoped `GarminRun` with token persistence
    (uses garminconnect's native `tokenstore` arg), 2.5 s rate-limit floor,
    cache-first / API-fallback `fetch_metric`.
  - `pipeline_shared.schema` — extra tables (`notifications`, `transactions`,
    `bank_imports`, `derived_daily`, `rolling_7d`, `rolling_30d`,
    `anomaly_events`, `pipeline_runs`).
  - `pipeline_shared.derived` — daily summary + 7/30-day rolling aggregates SQL.
  - `pipeline_shared.anomaly` — rule-based detection writing to
    `anomaly_events` + `notifications`.
  - `pipeline_shared.notifier` — drain queue + ntfy push.
  - `pipeline_shared.banking` — Playwright runner (sync + async), split
    `bank_login_and_pause` / `bank_resume_and_download` so a long human-OTP
    wait can sit in the workflow body between two short browser sessions
    (cookies serialised across).
  - `pipeline_shared.x_bookmarks` — OAuth 2.0 PKCE + bookmarks fetch +
    thread/quote resolution.
  - `pipeline_shared.cli` — `init / seed / reparse / derive / detect /
    notify / dates / status / x-init / x-exchange / x-bookmarks`.
- `pipelines/mock-bank/` — FastAPI fake bank (login → OTP → CSV download)
  used by all three pipelines' banking tests.
- `pipelines/dbos/` — async DBOS workflows. `DBOS.recv_async("otp")` inside
  the workflow body durably pauses for the human; split phase-1 / phase-2
  steps with serialised cookies in between. Scheduled `garmin_daily_tick`
  + `notifier_tick` via `@DBOS.scheduled`.
- `pipelines/dagster/` — daily-partitioned assets (7 raw + heart_rate_samples
  + derived_daily + anomaly_candidates) + asset-checks ported from
  garmin-fetch's `validate.py`. Banking implemented as
  `bank_login_job` → `bank_pending` table row → `bank_otp_sensor` poll →
  `bank_resume_job` (the eval doc's recommended Dagster pattern for
  awakeable-style flows).
- `pipelines/restate/` — `GarminIngest` service (handlers) + `BankImport`
  workflow with `ctx.awakeable()` for the human OTP. Registered with a
  separate `restate-server` container; both expose a sidecar FastAPI on
  8001 for OTP-approval and `/trigger/*` endpoints.

### Infra & docker
- New `docker-compose.pipelines.yml` containing `mock-bank`, `pipeline-dbos`,
  `pipeline-dagster`, `restate-server`, `pipeline-restate`.
- New `dagster-home`, `dagster-garth`, `dagster-garminconnect`,
  `restate-data` named volumes — pipeline state survives image rebuilds.
- `config/pipeline-*.env.tmpl` templates rendered by `just rs` (1Password CLI).
- 3 new Postgres databases on shared `postgres` instance:
  `pipeline_dbos`, `pipeline_dagster`, `pipeline_restate` (plus DBOS's
  system DB `pipeline_dbos_dbos_sys`).
- Justfile additions: `up-{dbos,dagster,restate}`, `pipeline-seed`,
  `pipeline-reparse`, `pipeline-status`, `bank-import-{tool}`, `mock-otp`,
  `pipeline-x-init/-x-exchange/-x-fetch`, `restate-register`.

### Inspection / dashboards
- DBOS sidecar dashboard reworked: `/` (cards-style status), `/runs`,
  `/transactions`, `/anomalies`, `/notifications`, `/x-status` — pulls
  workflow state from `pipeline_dbos_dbos_sys.dbos.workflow_status`.
- Dagster: native Dagit UI on `localhost:18802`, approval sidecar on `:18812`.
- Restate: native admin UI on `localhost:18803/ui/`, sidecar on `:18813`.
- Inspection guide: `2026-05-18-inspecting-pipelines.md`.

### Dagster productionisation (2026-05-19)
Settled on Dagster as the live pipeline. Tightened it up for real use.

- `pipelines/dagster/dagster.yaml` — `QueuedRunCoordinator` with
  `tag_concurrency_limits[{key=dagster/concurrency_key, value=garmin_api,
  limit=1}]`. The backfill GraphQL mutation propagates that key into each
  partition's run tags, so a 138-partition backfill executes as 138 strictly
  serial runs.
- `Definitions(executor=in_process_executor)` — all ops within a run share
  one Python process so the cached `GarminRun._client` is actually
  reusable (the prior multiprocess default re-imported everything per op).
- `@asset(retry_policy=GARMIN_RETRY)` with 3 retries × 30 s exponential +
  jitter on every raw_* asset — recovers transparently from Garmin auth
  rate limits.
- `pipeline_dagster_proj.resources` — module-level `_RUN_CACHE: dict[run_id
  → GarminRun]`, so all 10 ops in a single run materialise via the same
  `GarminRun` instance (= one login per partition, not seven).
- `pipeline_shared.garmin.GarminRun.get_client` — uses
  `Garmin().login(tokenstore=GARMIN_TOKEN_DIR)`. Library handles persistent
  OAuth refresh tokens to disk; subsequent runs short-circuit the whole
  `mobile+cffi → widget+cffi → portal+cffi` cascade.
- `_should_force_api(partition_key)` — for today + yesterday partitions
  the asset passes `force_api=True` to `fetch_metric`, bypassing the
  cache so each scheduled fire refreshes the in-progress and the
  late-arriving-sleep partitions. Older partitions take the cache fast-path.
- `garmin_daily_schedule` — `0 8,14,22 * * *`, `default_status=STOPPED` in
  code so each rebuild starts STOPPED; turn on from the UI when ready.

### Backfill of 2026
- Seeded `pipeline_dagster.raw_responses` from the canonical `garmin.raw_responses`
  (840 rows, 96 days from the existing `garmin-fetch` cron).
- Launched asset-backfill `niybavif` for 2026-03-27 → 2026-05-19 via
  GraphQL mutation (53 partitions: gap-day plus a 2-day overlap).
- 53/53 SUCCESS, 0 failures, 10.6 min total wall time.
  - Cache-hit partition: ~1.0–1.2 s
  - First cache-miss partition: 166.6 s (bootstraps the token cache through
    a 429-driven login cascade; `body_battery` UP_FOR_RETRY → `heart_rate`
    second login attempt succeeded → tokens dumped → `body_battery`
    retry succeeded on the now-warm client).
  - Subsequent cache-miss partitions: ~18 s each (7 API calls × 2.5 s
    rate-limit gate, zero auth roundtrips).
- 24 anomalies detected over the year (12 `low_sleep_score`, 3
  `very_short_sleep` critical, 4 z-score outliers, etc.). 133 days
  fully covered (all 6 parseable metrics).

### Schedule live
- `garmin_daily_schedule` started via the Dagit UI. Next fire 08:00 UTC.
- Each tick materialises today + yesterday partitions (forced live fetch);
  ~40 s total per fire.
- The original `garmin-fetch` cron is now redundant — can be torn down
  whenever you're satisfied with a few daily ticks of the Dagster pipeline.

### Architecture docs
- `2026-05-18-data-pipelines-plan.md` — overall plan.
- `2026-05-18-pipeline-{dagster,restate,dbos}.md` — per-tool plans.
- `2026-05-18-pipelines-verification.md` — what was built + how to test.
- `2026-05-18-inspecting-pipelines.md` — UIs + SQL recipes.

### Minimal upstream change
- Added a 5-line `[build-system]` + `[tool.hatch]` block to
  `/projects/garmin-fetch/pyproject.toml` so the project can be installed
  as a dependency. No behaviour change.
