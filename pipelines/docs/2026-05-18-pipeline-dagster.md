# Pipeline Plan: Dagster

Companion to `2026-05-18-data-pipelines-plan.md`. Reads the general contract from there; this file only covers Dagster-specific choices.

## Fit assessment

- **ETL (Garmin + Calendar + Derived)**: native. Daily-partitioned assets is exactly the abstraction.
- **Banking durable workflow**: stretched. Dagster doesn't have first-class `awakeable`. Workarounds: a sensor polling a `pending_approvals` table, or split the workflow across multiple assets/jobs gated by sensors. Either way the code shape is awkward compared to Restate/DBOS.

## Topology

Single `dagster dev` container on devserver. Sufficient for non-production homelab use; eval already accepted this.

```
pipeline-dagster/
  pyproject.toml              # dagster, dagster-postgres, pipeline-shared (editable)
  workspace.yaml
  dagster.yaml                # postgres storage config
  src/pipeline_dagster/
    __init__.py               # Definitions: assets + schedules + sensors + jobs
    resources.py              # GarminAPIResource, GarminStoreResource, CalendarResource, NtfyResource
    assets/
      garmin_raw.py           # 8 raw assets (7 metrics + stats) + heart_rate_samples
      garmin_parsed.py        # 7 parsed assets
      calendar.py             # raw_calendar + calendar_events
      derived.py              # rolling_7d, rolling_30d, sleep_hrv_daily, meeting_load_vs_recovery, activity_vs_sleep
      anomalies.py            # anomaly_candidates asset → writes notifications rows
    checks/
      garmin_checks.py        # @asset_check ports from validate.py
    schedules.py              # daily_garmin_schedule (3x), daily_calendar_schedule (1x)
    sensors/
      banking_approval.py     # polls pending_approvals, advances workflows
      notifier.py             # polls notifications, pushes to ntfy
    banking/
      jobs.py                 # ingest_transactions_job, finalize_transaction_job
```

## Asset definitions

`DailyPartitionsDefinition(start_date="2025-01-01")` for everything.

**Raw layer** (`group_name="raw"`):
- `raw_stats`, `raw_sleep`, `raw_heart_rate`, `raw_hrv`, `raw_stress`, `raw_body_battery`, `raw_steps`, `raw_training_readiness`, `raw_activities`, `raw_heart_rate_samples`

Each calls `garmin_api.get_client().get_<metric>_data(date_str)` and writes via `garmin_store.store_raw_response`. Identical shape across all 10.

**Parsed layer** (`group_name="parsed"`):
- `parsed_sleep` ← `raw_sleep + raw_stats`
- `parsed_stress` ← `raw_stress + raw_stats`
- `parsed_steps` ← `raw_steps + raw_stats`
- `parsed_heart_rate`, `parsed_hrv`, `parsed_body_battery`, `parsed_training_readiness` ← single raw input
- `parsed_heart_rate_samples` ← `raw_heart_rate_samples + raw_activities`

Each reads from `raw_responses`, calls existing `_parse_<metric>` from `pipeline-shared` (re-exported from `garmin-fetch/fetcher.py`), upserts via existing `GarminStore` method.

**Calendar** (`group_name="calendar"`):
- `raw_calendar` (trailing-7-day window — uses `MultiPartitionsDefinition` or a single daily asset that fetches a window)
- `calendar_events` (parsed)

**Derived** (`group_name="derived"`):
- `rolling_7d_metrics`, `rolling_30d_metrics` — read trailing window from parsed tables, write to derived tables.
- `sleep_hrv_daily` — join sleep + hrv + training_readiness on date.
- `meeting_load_vs_recovery` — join calendar_events + hrv + stress.
- `activity_vs_sleep` — join activities + next-day sleep.

These are daily-partitioned but `AutoMaterializePolicy.eager()` so they refresh whenever upstream lands.

**Anomalies** (`group_name="alerts"`):
- `anomaly_candidates` — daily-partitioned, reads `rolling_30d_metrics` for baseline + today's parsed values. Rules: z-score > 2.5, sleep_score < 50, hrv < baseline_low. Inserts rows into `notifications`.

## Asset checks (validation)

Each parsed asset gets `@asset_check` ports of `validate.py:7-43` logic:
```python
@asset_check(asset=parsed_sleep)
def sleep_no_nulls(context, garmin_store: GarminStoreResource):
    # check NULLs and zeros for partition_key
    return AssetCheckResult(passed=..., metadata={...})
```
Runs automatically after materialization. Results visible per-asset in the UI.

## Schedules

```python
@schedule(cron_schedule="0 8,14,22 * * *", job=garmin_daily_job)
def garmin_daily_schedule(context):
    today = date.today()
    yesterday = today - timedelta(days=1)
    return [
        RunRequest(partition_key=today.isoformat()),
        RunRequest(partition_key=yesterday.isoformat()),
    ]

@schedule(cron_schedule="0 9 * * *", job=calendar_daily_job)
def calendar_daily_schedule(context):
    # last 7 days
    return [RunRequest(partition_key=d.isoformat()) for d in trailing_7_days()]
```

## Late-arriving rule

**Approach (b) — sensor**, contrary to the eval doc's lean toward (a). Reasoning: the rule is cheap to express as a `should_fetch` check inside the raw asset, and we already have it in `store.py:434-456`. So:
- Raw asset body calls `garmin_store.should_fetch(table, date_str)` before hitting API.
- If `False`, returns existing data with `MaterializeResult(metadata={"skipped": "fresh enough"})`.
- The schedule always requests today + yesterday; `should_fetch` decides per-asset whether to hit the API.

Net effect: same behavior as today, but visible per-asset in UI (skipped vs fetched runs).

## Backfill

Native: Dagster UI → select asset → "Materialize" → date range picker. Per-partition retry. Concurrency limit (1 at a time for Garmin assets via `tags={"dagster/concurrency_key": "garmin_api"}`) to respect the 1-req-sec rate limit shared across partitions.

CLI fallback: `dagster asset materialize --select 'parsed_*' --partition-range 2025-01-01..2025-03-31`.

## Failure surfacing

- **429 / rate limit / transient**: retry policy `RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL, jitter=Jitter.PLUS_MINUS)` on each raw asset.
- **401/403 auth / CAPTCHA**: raise a typed exception in the resource; Dagster marks the run failed. A failure sensor (`@run_failure_sensor`) writes a row into `notifications` with severity=critical → ntfy push.
- **`stats` failure**: cascades to dependent assets via missing input.

## Banking workflow (the awkward part)

Modeled as two jobs + a sensor:

1. **`ingest_transactions_job`** — scheduled / webhook-triggered. For each new tx:
   - Insert into `transactions` table with status=`pending` or `auto_approved`.
   - If amount > threshold: also insert into `pending_approvals(id, tx_id, token, created_at, decision, decided_at)`, and write a `notifications` row with kind=`approval_needed` + the approval URL.
2. **`finalize_transaction_job`** — triggered per-instance by `banking_approval_sensor`:
   - Polls `pending_approvals` every 30s for rows where `decision IS NOT NULL` and the tx hasn't been finalized.
   - For each, kicks off `finalize_transaction_job` with `tx_id` in the run config.
   - Job marks the tx committed or rejected.

**Honest assessment**: this works, but it's the "manually rebuild durable execution" pattern. State lives in a Postgres table, the sensor is the resumption mechanism, and the workflow logic is split across job boundaries. Code that would be 15 lines in Restate/DBOS is ~80 lines here, and you give up clean "I'm paused waiting for a thing" semantics in the UI — the run finished, the *thing* is in a separate table.

Approval URL endpoint (Flask/FastAPI sidecar in same container? or use Dagster's webserver routes? unclear — prototype likely starts with a tiny FastAPI service in the same container that just updates `pending_approvals.decision`).

## Notifier

Cheapest path: `@sensor` on `notifications` table.
```python
@sensor(minimum_interval_seconds=30)
def ntfy_dispatch_sensor(context, ntfy: NtfyResource):
    new = fetch_undelivered(severity_at_least="warn")
    for row in new:
        ntfy.publish(row.payload)
        mark_delivered(row.id)
```

## Docker / Caddy / Secrets

```yaml
# docker-compose.yml addition
pipeline-dagster:
  image: ankit/devbox:1.3
  working_dir: /workspace/pipeline-dagster
  command: dagster dev -h 0.0.0.0 -p 3000
  environment:
    DAGSTER_HOME: /workspace/.dagster_home
  env_file: ./secrets/pipeline-dagster.env
  volumes:
    - .:/workspace
    - dagster-home:/workspace/.dagster_home
  networks: [mybridge]
  depends_on: [postgres]

volumes:
  dagster-home:
```

Caddy route: `dagster.home.ankitson.com → pipeline-dagster:3000`.

`config/pipeline-dagster.secrets.env.tmpl`:
```
GARMIN_EMAIL={{ op://clankers/garmin/email }}
GARMIN_PASSWORD={{ op://clankers/garmin/password }}
DATABASE_URL=postgresql://garmin:{{ op://... }}@postgres:5432/pipeline_dagster
NTFY_TOPIC={{ op://clankers/ntfy/topic }}
NTFY_TOKEN={{ op://clankers/ntfy/token }}
```

Justfile: `up-dagster`, `down-dagster`, `dagster-shell`, `dagster-backfill`.

## Concrete shortcomings

1. **Banking workflow is a forced fit.** Sensor + table-driven state is the right Dagster-native pattern but it's the wrong abstraction for this work. Approval requests get split from approval handlers in the UI.
2. **Resource footprint**: 300-500 MB RAM for a pipeline whose hot path runs ~90 seconds 3x/day. Eval doc flagged this.
3. **`dagster dev` is single-process** — not production-grade. Acceptable for devserver evaluation; would need split webserver+daemon+code-location for homeserver promotion.
4. **Per-partition concurrency control on Garmin API** must be configured carefully — easy to accidentally fan out and trip rate limits during a backfill.

## Verification

```bash
just up-dagster
# UI at https://dagster.home.ankitson.com
# Materialize today's partition for all parsed_* assets → confirm tables populated
# Trigger 7-day backfill on raw_sleep → kill container mid-run → restart → confirm resume
# Insert anomaly: UPDATE sleep SET sleep_score=0 WHERE date='today'
# Re-materialize anomaly_candidates for today → confirm notifications row + ntfy push
# POST a fake $1500 transaction → confirm ntfy with approval URL → click approve
# → confirm transaction committed, notifications updated
```
