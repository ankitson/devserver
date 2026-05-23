# Inspecting Pipeline Status

Each pipeline exposes its own web UI on `localhost`. All three are on the
`mybridge` Docker network — if you forward through Tailscale / Caddy, the
same URLs work remotely.

## Quick links

| Pipeline | What | URL |
|---|---|---|
| DBOS | custom dashboard (data + workflows + anomalies + txns) | http://localhost:18801 |
| Dagster | native Dagit UI (assets, runs, partitions, schedules, sensors) | http://localhost:18802 |
| Restate | native Restate admin UI (services, deployments, invocations) | http://localhost:18803/ui/ |
| Mock bank | login + statements page (test only) | http://mock-bank:8000 (Docker network only) |

Approval endpoints (used by the human-in-loop OTP flow):

| Pipeline | URL pattern |
|---|---|
| DBOS | http://localhost:18801/approve?wf=<workflow_id> |
| Dagster | http://localhost:18812/dagster-approve?pending_id=<id> |
| Restate | http://localhost:18813/restate-approve?otp_id=<awakeable_id> |

## DBOS (port 18801)

DBOS has no native OSS dashboard, so this is a hand-rolled FastAPI sidecar.
All pages share a top navbar.

- **`/`** — Dashboard. Cards for each parsed table (sleep, heart_rate, hrv,
  derived_daily, anomalies, raw cache), transaction summary by status,
  undelivered notifications, DBOS workflow status counts, and the last 10
  failures from `pipeline_runs`. One-click forms to trigger Garmin
  `fetch_day`/`fetch_window` and bank import.
- **`/runs`** — Filterable list of DBOS workflows (workflow_uuid, status,
  name, error). Reads from `pipeline_dbos_dbos_sys.dbos.workflow_status`.
  Filters: `?name=&status=PENDING|SUCCESS|ERROR&limit=N`.
- **`/transactions`** — Per-status totals + last 200 transactions
  (auto-committed vs `pending_approval`).
- **`/anomalies`** — Last 100 detected anomalies (date, metric, kind,
  severity, value, baseline, z-score, rule).
- **`/notifications`** — Last 100 notification rows with delivery status.

Trigger endpoints (POST forms on the dashboard):

```
/trigger/fetch_day        date=YYYY-MM-DD
/trigger/fetch_window     start=… end=…
/trigger/derive           date=…
/trigger/detect           date=…
/trigger/bank_import      bank_name=mock-bank username=… password=…
```

## Dagster (port 18802)

The full native Dagit UI. Things to look at:

- **Assets** (sidebar → Assets) — partition grid for each Garmin asset.
  Green/red squares show which dates have materialized successfully.
  Drill into any cell to see logs, metadata, asset checks, and lineage.
- **Runs** (sidebar → Runs) — last N runs across all jobs, with logs.
- **Asset Checks** (sidebar → Asset Checks) — pass/fail history of the
  `validate_sleep`, `validate_heart_rate`, `validate_hrv` ports of
  `validate.py`. Per partition.
- **Schedules** (sidebar → Schedules) — `garmin_daily_schedule` (8/14/22
  UTC). Status, tick history, next planned tick.
- **Sensors** (sidebar → Sensors) — `bank_otp_sensor`, `notifier_sensor`.
  Tick history (the bank sensor polls every 3s).
- **Backfills** (sidebar → Backfills) — partition-range materializations
  with per-date progress.
- **Jobs** — `garmin_full_job`, `bank_login_job`, `bank_resume_job`.

Tip: use the Asset Catalog filter `group:garmin_raw` / `group:garmin_derived`
/ `group:garmin_alerts` to slice the view.

## Restate (port 18803/ui/)

Native Restate admin UI. Tabs (top of page):

- **Services** — `GarminIngest`, `BankImport`, `Notifier`. Each lists
  registered handlers, input/output JSON schemas, retry policy.
- **Invocations** — every workflow / handler invocation with status
  (Running, Suspended, Completed, Failed). Click into one to see the full
  journal: every `ctx.run(...)`, every `ctx.awakeable(...)`, every
  `service_call(...)`. This is the best view for understanding what a
  paused workflow is actually waiting on.
- **Deployments** — the SDK deployment URL (http://pipeline-restate:9080).
- **Awakeables** — paused awakeables (those waiting for OTP show up here).

If you'd rather hit JSON directly:

```bash
# all registered services
curl http://localhost:18803/services

# all invocations (admin API)
curl http://localhost:18803/query | jq

# resolve an awakeable from the CLI:
curl -X POST http://localhost:18803/awakeables/<id>/resolve \
  -H 'content-type: application/json' -d '"123456"'
```

## Cross-cutting: SQL queries

Sometimes the SQL is faster than clicking around. Pick the database
based on which pipeline you want to inspect (`pipeline_dbos`,
`pipeline_dagster`, `pipeline_restate`).

```bash
# Open a psql shell
just _psql() { docker exec -it postgres psql -U garmin -d "$1"; }
just _psql pipeline_dbos
```

Useful one-liners:

```sql
-- which days do we have data for?
SELECT MIN(date), MAX(date), COUNT(*) FROM sleep;

-- which (date, metric) pairs failed parsing?
SELECT date, metric FROM raw_responses r
 WHERE NOT EXISTS (
   SELECT 1 FROM sleep WHERE date = r.date AND r.metric='sleep'
 ) AND metric IN ('sleep','heart_rate','hrv','stress','body_battery',
                  'steps','training_readiness')
 ORDER BY date DESC LIMIT 50;

-- recent pipeline_runs by status
SELECT status, COUNT(*), MAX(finished_at)
  FROM pipeline_runs
 GROUP BY status ORDER BY status;

-- recent failures (any pipeline writes here)
SELECT asset, partition_key, status, LEFT(error, 200) AS err, started_at
  FROM pipeline_runs
 WHERE error IS NOT NULL OR status LIKE 'error%'
 ORDER BY started_at DESC LIMIT 20;

-- DBOS workflow status (DBOS only, lives in *_dbos_sys DB)
\c pipeline_dbos_dbos_sys
SELECT status, name, COUNT(*) FROM dbos.workflow_status
 GROUP BY status, name ORDER BY name;

-- which transactions are pending approval?
SELECT external_id, posted_date, amount_cents/100.0 AS amount,
       merchant, category
  FROM transactions WHERE status='pending_approval'
 ORDER BY amount_cents DESC;

-- undelivered notifications
SELECT id, severity, kind, title, body, created_at
  FROM notifications WHERE delivered_at IS NULL
 ORDER BY created_at;
```

## "What does each pipeline say about today?"

A quick comparison query (run from anywhere with access to postgres):

```sql
WITH d AS (SELECT CURRENT_DATE - 3 AS d)
SELECT 'dbos'    AS pipeline,
       (SELECT sleep_score FROM pipeline_dbos.public.sleep WHERE date=d.d) AS sleep,
       (SELECT resting    FROM pipeline_dbos.public.heart_rate WHERE date=d.d) AS hr
FROM d
UNION ALL
SELECT 'dagster',
       (SELECT sleep_score FROM pipeline_dagster.public.sleep WHERE date=d.d),
       (SELECT resting    FROM pipeline_dagster.public.heart_rate WHERE date=d.d)
FROM d
UNION ALL
SELECT 'restate',
       (SELECT sleep_score FROM pipeline_restate.public.sleep WHERE date=d.d),
       (SELECT resting    FROM pipeline_restate.public.heart_rate WHERE date=d.d)
FROM d;
```

(Postgres can't cross-DB join without dblink/FDW — easiest is to run the
same query three times via `\c pipeline_<tool>`.)

## Failure triage workflow

When something is wrong, in order:

1. **Open the pipeline's web UI** and look at recent failures.
   - DBOS dashboard → "Recent failures" table on `/` (also `/runs?status=ERROR`).
   - Dagster → Runs page filtered to failed.
   - Restate → Invocations tab filtered to Failed/Suspended.
2. **Click into the failing run** for stderr / journal.
3. **Cross-check `pipeline_runs`** — the cross-cutting log table populated
   by every pipeline. Same shape regardless of tool.
4. **For Garmin specifically**: check `raw_responses` to see if the API
   actually returned data. An empty `{}` raw response means Garmin had
   nothing for that day (e.g. training_readiness on rest days).
5. **For banking**: check `bank_imports` for the row's `status` and
   `error`; check `bank_pending` (Dagster) or DBOS workflow status for
   the awaiting-OTP state.

## Health checks

```bash
curl http://localhost:18801/healthz             # DBOS
curl http://localhost:18802/server_info         # Dagster
curl http://localhost:18803/version             # Restate
curl http://localhost:18813/healthz             # Restate sidecar
curl http://localhost:18812/healthz             # Dagster sidecar
```

All five should return 200 with JSON.
