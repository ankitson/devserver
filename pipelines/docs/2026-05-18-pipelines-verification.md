# Pipelines Verification (2026-05-18)

All three implementations (DBOS, Dagster, Restate) of the Garmin + banking
pipeline run end-to-end against the existing shared Postgres on `mybridge`.

## Topology that came up

| Service | Container | Network | URL |
|---|---|---|---|
| Mock bank | `mock-bank` | mybridge | http://mock-bank:8000 |
| DBOS app + UI | `pipeline-dbos` | mybridge | http://localhost:18801 |
| Dagster UI | `pipeline-dagster` | mybridge | http://localhost:18802 |
| Dagster approval sidecar | `pipeline-dagster` | mybridge | http://localhost:18812 |
| Restate server admin | `restate-server` | mybridge | http://localhost:18803 |
| Restate ingress | `restate-server` | mybridge | http://localhost:18883 |
| Restate approval sidecar | `pipeline-restate` | mybridge | http://localhost:18813 |

Each pipeline writes to its own Postgres DB on the shared `postgres` instance:
`pipeline_dbos`, `pipeline_dagster`, `pipeline_restate` (plus
`pipeline_dbos_dbos_sys` for DBOS state).

## What was verified

### 1. Garmin reparse against existing raw cache
All three pipelines seeded `raw_responses` from the canonical `garmin` DB
(840 rows over 96 days, populated by the existing `garmin-fetch` cron job)
and re-parsed without any external API call.

Spot-check, 2026-05-15 sleep across all four DBs:

```
SOURCE  : 45 | POOR | 26700
DBOS    : 45 | POOR | 26700
Dagster : 45 | POOR | 26700
Restate : 45 | POOR | 26700
```

Identical. The shared library uses the existing `garmin-fetch` parser code,
so all three pipelines produce the same parsed output as the upstream cron
job — by construction.

### 2. Derived layer + anomaly detection
`derived_daily`, `rolling_7d`, `rolling_30d`, `anomaly_events`,
`notifications` all populated correctly. The 2026-05-15 sleep_score of 45
triggered the `low_sleep_score` rule (severity=warn) on all three pipelines.

### 3. Banking workflow with human-in-the-loop OTP
A single bank import (DBOS workflow / Dagster job-pair / Restate workflow)
each:
- Drove Playwright into the mock bank (login form → OTP page)
- Durably paused waiting for a human OTP
- Resumed after the human (via approval form) supplied the OTP
- Downloaded the statement CSV
- Processed 24 transactions: 22 auto-committed, 2 flagged `pending_approval`
  (amounts ≥ $100)

Identical counts and totals across all three pipelines.

| Pipeline | Pause mechanism | Resume mechanism |
|---|---|---|
| DBOS | `DBOS.recv_async("otp")` inside async workflow | `DBOS.send(wf_id, otp, topic="otp")` from FastAPI handler |
| Dagster | Insert `bank_pending` row with `otp=NULL`; job ends | `bank_otp_sensor` polls every 3s, fires `bank_resume_job` when `otp` is set |
| Restate | `id, promise = ctx.awakeable(); await promise` | `POST /restate/awakeables/{id}/resolve` via ingress |

### 4. Conservative Garmin rate limit
`GARMIN_LIVE_FETCH=false` by default in every secrets template — pipelines
never call the Garmin API unless explicitly opted in. When the flag is on,
`pipeline_shared.garmin._RateLimiter` enforces a process-wide 2.5s floor
(higher than `garmin-fetch`'s own 1s) and the existing `@with_retry` + 30s
429 backoff is preserved unchanged.

## Files of interest

```
pipelines/shared/         # domain library (re-uses /projects/garmin-fetch)
  src/pipeline_shared/
    garmin.py             # GarminRun, reparse_metric, reparse_day, fetch_metric
    derived.py            # rolling_7d/30d + cross-metric joins
    anomaly.py            # rule-based detection
    banking.py            # bank_login_and_pause + bank_resume_and_download
    notifier.py           # notifications table + ntfy push
    seed.py               # copy raw_responses from source garmin DB
    cli.py                # init / seed / reparse / derive / detect / status

pipelines/mock-bank/      # fake bank for testing (FastAPI)

pipelines/dbos/           # DBOS workflows + FastAPI sidecar
pipelines/dagster/        # Dagster assets, jobs, sensors + sidecar
pipelines/restate/        # Restate services + sidecar
```

## How to reproduce

```bash
# from /home/ankit/hroot/devserver
just up-dbos             # mock-bank + pipeline-dbos
just up-dagster
just up-restate

# Seed each pipeline's raw_responses from the canonical garmin DB
just pipeline-seed dbos     2026-05-12 2026-05-18
just pipeline-seed dagster  2026-05-12 2026-05-18
just pipeline-seed restate  2026-05-12 2026-05-18

# Reparse one day on a pipeline (uses cache, no Garmin API)
just pipeline-reparse dbos 2026-05-15
just pipeline-status  dbos

# Trigger a bank import on a pipeline
just bank-import-dbos
# … then approve in the UI sidecar using:
just mock-otp                 # prints the test-mode OTP
```

## Honest assessment

- **DBOS**: cleanest code, single container, all state in the same Postgres
  the pipeline already needs. The `recv_async` pattern is exactly what the
  bank flow wants. Only annoyance: needs two databases on Postgres
  (`pipeline_dbos` + `pipeline_dbos_dbos_sys`).
- **Dagster**: heaviest stack (webserver + daemon + grpc code server inside
  one container running `dagster dev`). Asset graph is overkill for 7
  metrics but the partition UI, asset checks, and backfill ergonomics are
  much nicer than the other two. Banking is the awkward part — sensor +
  state table is the right Dagster-native pattern but it's clearly a
  workaround for the missing durable-pause primitive.
- **Restate**: cleanest banking workflow (the `awakeable` is exactly the
  primitive needed, two lines of code), and reparse is fine but the ETL
  ergonomics are bare — no partition UI, no per-asset lineage, you write
  date loops yourself. Two containers (server + SDK app) for the privilege
  of a much smaller programming model.

## Known shortcomings of this build

- Telemetry tables (`pipeline_runs`) are populated by each pipeline but
  there's no UI for them yet. SQL-only.
- ntfy push is wired in `Notifier.drain_once()` but `NTFY_TOPIC` is empty
  by default. Set it in the secrets template to enable.
- The mock bank stores OTPs in memory; container restart drops all
  sessions. Adequate for testing, not production.
- The `current_otp` test endpoint exists in mock-bank to let the approval
  UI display the OTP back to the tester. Remove for real banks.
- `garmin-fetch`'s pyproject was minimally updated to add a `[build-system]`
  + `[tool.hatch]` block so it can be installed as a dependency. No
  behavior change.
