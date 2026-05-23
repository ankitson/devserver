# Pipeline Plan: DBOS

Companion to `2026-05-18-data-pipelines-plan.md`. Reads the general contract from there; this file only covers DBOS-specific choices.

## Fit assessment

- **Banking durable workflow**: strong. `@DBOS.workflow` + `recv()` / `send()` for human approval is natural; transactional commit between workflow checkpoint and DB write is first-class.
- **ETL (Garmin + Calendar + Derived)**: workable but hand-rolled (like Restate). No asset graph; you write workflows.
- **Operational footprint**: cleanest of the three. No webserver, no daemon, no metadata DB — DBOS lives as a library inside one Python process, with state in the same Postgres you already use.

## Topology

Single Python process running DBOS as a library, exposing one FastAPI app for HTTP triggers (banking webhooks, approval endpoints, manual triggers). DBOS state lives in the *same* Postgres database as the pipeline data, in DBOS-managed tables.

```
pipeline-dbos/
  pyproject.toml              # dbos, fastapi, pipeline-shared (editable)
  dbos-config.yaml
  src/pipeline_dbos/
    main.py                   # FastAPI app + DBOS init + scheduled workflow registration
    workflows/
      garmin.py               # fetch_day, fetch_window workflows
      calendar.py             # calendar workflows
      derived.py              # derived layer workflows
      banking.py              # approve_transaction workflow + send_event endpoint
      notifier.py             # notifier workflow
    steps.py                  # @DBOS.step wrappers around pipeline-shared functions
    transactions.py           # @DBOS.transaction wrappers for DB writes
```

## Workflow definitions

### Garmin

```python
@DBOS.workflow()
def fetch_day(date_str: str) -> dict[str, str]:
    client = login_step()  # @DBOS.step, run once per workflow
    results = {}
    for metric in METRICS:
        if not should_fetch_step(metric, date_str):
            results[metric] = "skipped"
            continue
        try:
            raw = fetch_raw_step(client, metric, date_str)  # @DBOS.step, durable
            store_raw_txn(metric, date_str, raw)            # @DBOS.transaction
            parsed = parse_step(metric, raw)                # pure, but step for durability
            if parsed:
                upsert_parsed_txn(metric, date_str, parsed)  # @DBOS.transaction
            results[metric] = "ok"
        except Exception as e:
            results[metric] = f"error: {e}"
            log_failure_txn(metric, date_str, str(e))
    return results

@DBOS.workflow()
def fetch_window(start: str, end: str):
    for d in date_range(start, end):
        DBOS.start_workflow(fetch_day, d)  # forks child workflow, durably
```

### Scheduled triggers

DBOS has `@DBOS.scheduled(cron_expression)` decorator — workflow is enqueued at each cron tick, the runtime guarantees exactly-once trigger semantics:

```python
@DBOS.scheduled("0 8,14,22 * * *")
@DBOS.workflow()
def daily_garmin_tick(scheduled_time, actual_time):
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    DBOS.start_workflow(fetch_day, today)
    if should_refetch_yesterday():
        DBOS.start_workflow(fetch_day, yesterday)
```

### Derived layer

Chained from `fetch_day` by calling `DBOS.start_workflow(refresh_derived, date_str)` at the end. Each derived step is a `@DBOS.transaction` so it runs in the same Postgres transaction as its own commit.

### Anomaly + notification

Same pattern: workflow reads parsed + rolling baseline, inserts into `notifications` via `@DBOS.transaction`.

### Banking (DBOS sweet spot)

```python
@DBOS.workflow()
def approve_transaction(tx: Transaction):
    category = categorize_step(tx)
    if tx.amount > THRESHOLD:
        DBOS.set_event("approval_token", DBOS.workflow_id)
        send_approval_push_step(tx, DBOS.workflow_id)
        decision = DBOS.recv("approval_decision", timeout_seconds=86400 * 7)  # 7 days
        if decision is None or not decision["approved"]:
            mark_rejected_txn(tx)
            return
    commit_transaction_txn(tx, category)
```

External approval endpoint (FastAPI):
```python
@app.post("/approve/{wf_id}")
async def approve(wf_id: str, approved: bool):
    DBOS.send(wf_id, {"approved": approved}, topic="approval_decision")
    return {"ok": True}
```

`DBOS.recv` durably blocks. Workflow state survives process restarts because every step is checkpointed to Postgres. No background process held open for the duration.

**Extra win**: `commit_transaction_txn` is a `@DBOS.transaction` — the workflow checkpoint and the balance update commit in the same Postgres transaction. Cannot end up with "money moved but workflow forgot it moved money."

## Late-arriving rule

Inside `daily_garmin_tick` — plain Python `if should_refetch_yesterday(): ...`. Trivial.

## Backfill

No native UI. Options:
1. **HTTP endpoint** in the FastAPI sidecar: `POST /backfill {start, end}` → `DBOS.start_workflow(fetch_window, ...)`.
2. **CLI**: `uv run python -m pipeline_dbos.backfill 2025-01-01 2025-03-31`.
3. **DBOS Cloud UI** (paid) has a workflow list view. OSS does not.

Resumability: workflows that crash mid-execution resume from the last completed step on process restart. `fetch_window` that crashes on day 45 resumes from day 45.

For visibility: query DBOS's workflow tables directly in Postgres (`dbos.workflow_status`, `dbos.operation_outputs`).

## Failure surfacing

- **Per-step retries**: `@DBOS.step(retries_allowed=True, max_attempts=4, interval_seconds=2, backoff_rate=2.0)`.
- **Workflow-level failure**: workflow goes to `ERROR` status in `dbos.workflow_status`. A separate `failure_watcher` workflow (scheduled) polls for errored workflows and writes notifications.
- **CAPTCHA/auth**: typed exception → step fails permanently → workflow errors → notification fires.

## Validation

Per-metric validation as DBOS steps after upsert:
```python
validation = validate_metric_step(metric, date_str)
if not validation.passed:
    write_validation_failure_txn(metric, date_str, validation.errors)
```

Validation results in a dedicated `validation_results` table; queried via DBOS Postgres or via FastAPI endpoints.

## Notifier

The cleanest pattern for DBOS:
```python
@DBOS.scheduled("* * * * *")  # every minute
@DBOS.workflow()
def dispatch_notifications(scheduled_time, actual_time):
    pending = fetch_undelivered_txn(severity_at_least="warn")
    for row in pending:
        push_ntfy_step(row)
        mark_delivered_txn(row.id)
```

## UI

DBOS OSS has no admin UI. We build a tiny FastAPI dashboard:
- `/runs` — last N workflows from `dbos.workflow_status`
- `/runs/{id}` — workflow detail with steps and outputs
- `/backfill` — form to trigger window backfill
- `/approvals/{wf_id}` — approval page for banking workflows

This is intentionally minimal — for a homelab the value is queryable SQL on `dbos.*` tables more than a fancy UI.

## Docker / Caddy / Secrets

```yaml
pipeline-dbos:
  image: ankit/devbox:1.3
  working_dir: /workspace/pipeline-dbos
  command: uv run uvicorn pipeline_dbos.main:app --host 0.0.0.0 --port 8000
  env_file: ./secrets/pipeline-dbos.env
  volumes: [.:/workspace]
  networks: [mybridge]
  depends_on: [postgres]
```

Single container. No second service. No daemon. No metadata DB (uses shared Postgres).

Caddy: `dbos.home.ankitson.com → pipeline-dbos:8000`.

`config/pipeline-dbos.secrets.env.tmpl` — GARMIN_*, DATABASE_URL (used by both pipeline data and DBOS state), NTFY_*.

DBOS config in `dbos-config.yaml`:
```yaml
name: pipeline-dbos
language: python
database:
  hostname: postgres
  port: 5432
  username: dbos_user
  password: ${DBOS_DB_PASSWORD}
  app_db_name: pipeline_dbos
```

## Concrete shortcomings

1. **No asset lineage view.** Same as Restate — you see workflows, not assets-with-dependencies. For a 50-asset pipeline this becomes painful.
2. **No backfill UI in OSS.** Build it yourself (small) or live with CLI.
3. **Workflow ID management for banking.** Approval URLs need the workflow ID; have to expose it cleanly when sending the push. (Solvable, just a wrinkle.)
4. **Less mature than Temporal/Dagster.** Smaller community, fewer Stack Overflow answers, but the model is simple enough that this is rarely a blocker.
5. **All state in one Postgres.** Operationally a win for backups (one thing to back up); operationally a risk if Postgres goes down (everything goes down). Acceptable for homelab.

## Verification

```bash
just up-dbos
# Dashboard at https://dbos.home.ankitson.com
# Manual trigger via curl: POST /trigger/fetch_day {"date":"2026-05-18"}
# → confirm raw_responses + parsed tables populated
# Kill container mid-fetch → restart → confirm workflow resumes (visible in dbos.workflow_status)
# Backfill via POST /backfill {"start":"...","end":"..."}
# Inject anomaly → next scheduled tick → confirm notifications row + ntfy
# Post test transaction > threshold → workflow pauses at DBOS.recv
# → ntfy received with approval URL → POST /approve/{wf_id} {"approved":true}
# → confirm workflow resumes, tx committed in same Postgres txn as workflow checkpoint
# Restart container while approve_transaction workflow is paused → confirm it picks up the send() correctly
```
