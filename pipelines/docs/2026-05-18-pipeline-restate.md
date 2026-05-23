# Pipeline Plan: Restate

Companion to `2026-05-18-data-pipelines-plan.md`. Reads the general contract from there; this file only covers Restate-specific choices.

## Fit assessment

- **Banking durable workflow**: native. `ctx.awakeable()` is the exact primitive for "pause until human approves" with no resident process.
- **ETL (Garmin + Calendar + Derived)**: hand-rolled. Restate has no asset graph, no partitioning, no backfill UI. You write handlers that loop dates and call `ctx.run()` for durability — workable but you're rebuilding what Dagster gives free.

## Topology

Single Restate server binary (Rust, embedded RocksDB) + one or more SDK service processes that register handlers. Python SDK chosen to match `pipeline-shared`.

```
pipeline-restate/
  pyproject.toml              # restate-sdk, pipeline-shared (editable)
  src/pipeline_restate/
    services/
      garmin_ingest.py        # service GarminIngest with handlers fetch_day, fetch_metric, backfill
      calendar_ingest.py      # service CalendarIngest
      derived.py              # service Derived: rollups, correlations, anomalies
      banking.py              # virtual object BankAccount + workflow ApproveTransaction
      notifier.py             # service Notifier with handler dispatch_pending
    cron.py                   # schedule definitions registered with Restate
    server.py                 # bootstraps the SDK app and binds 0.0.0.0:9080
```

## Service definitions

### GarminIngest (regular service, run-keyed for auth sharing)

```python
@service
class GarminIngest:
    @handler
    async def fetch_day(self, ctx: Context, date_str: str) -> dict[str, str]:
        # one logical "run" — share GarminClient session across metrics
        client = await ctx.run("login", lambda: garmin_login(EMAIL, PASSWORD))
        # for each metric, durable step:
        results = {}
        for metric in METRICS:
            results[metric] = await ctx.run(
                f"fetch_{metric}",
                lambda m=metric: fetch_and_upsert(client, m, date_str),
                retry_policy=RetryPolicy(initial_interval=1, max_interval=30, max_attempts=4),
            )
            await ctx.sleep(timedelta(seconds=1))  # rate limit
        return results

    @handler
    async def fetch_window(self, ctx: Context, start: str, end: str):
        for d in date_range(start, end):
            await ctx.service_call(GarminIngest.fetch_day, d)  # durable

    @handler
    async def daily_tick(self, ctx: Context):
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        await ctx.service_call(GarminIngest.fetch_day, today)
        if should_refetch_yesterday():  # before-10am-UTC rule
            await ctx.service_call(GarminIngest.fetch_day, yesterday)
```

Each `ctx.run(name, fn)` is durably journaled — on failure it retries with backoff; on crash mid-handler, on restart it resumes from the next un-journaled step.

### CalendarIngest

Same pattern; daily handler fetches trailing 7 days.

### Derived (service)

```python
@service
class Derived:
    @handler
    async def refresh_day(self, ctx: Context, date_str: str):
        await ctx.run("rolling_7d", lambda: compute_rolling_window(7, date_str))
        await ctx.run("rolling_30d", lambda: compute_rolling_window(30, date_str))
        await ctx.run("sleep_hrv_daily", lambda: compute_sleep_hrv_daily(date_str))
        await ctx.run("meeting_load", lambda: compute_meeting_load(date_str))
        await ctx.run("anomalies", lambda: detect_anomalies(date_str))
```

Triggered by chaining from `GarminIngest.fetch_day` and `CalendarIngest.daily_tick` — call `ctx.service_send(Derived.refresh_day, date_str)` at the end. `service_send` = fire-and-forget, separate transaction.

### Banking (virtual object + workflow — Restate's strong suit)

```python
@virtual_object
class BankAccount:
    """One instance per account_id. Serialized state mutations."""
    balance: int

    @handler
    async def apply_transaction(self, ctx: ObjectContext, tx: Transaction):
        category = await ctx.run("categorize", lambda: categorize(tx))
        if tx.amount > THRESHOLD:
            id, promise = ctx.awakeable()  # durable pause
            await ctx.run("notify", lambda: send_approval_push(tx, id))
            decision = await promise  # waits forever, survives restarts
            if not decision.approved:
                await ctx.run("mark_rejected", lambda: mark_rejected(tx))
                return
        new_balance = self.balance - tx.amount
        await ctx.run("commit_tx", lambda: commit_transaction(tx, category, new_balance))
        self.balance = new_balance
```

Approval endpoint (separate tiny FastAPI service or another Restate handler): receives the click on the approval URL, calls `ctx.resolve_awakeable(id, decision)`. Restate routes the resolution to the paused workflow.

**Key wins here vs other tools:**
- The pause is opaque to the caller — workflow code reads top-to-bottom.
- Per-account serialization is free (virtual object semantics).
- Survives Restate restarts trivially (state in RocksDB).

## Schedules

Restate supports cron-style triggers via the admin API or a `restate-cron` setup. Two options:
1. Register schedules at startup via the SDK (`cron.py`).
2. Use external cron in the container that POSTs to Restate's HTTP ingress.

Plan: option (1) for daily ticks, falling back to (2) if SDK cron is too limited.

```python
register_cron("0 8,14,22 * * *", GarminIngest.daily_tick)
register_cron("0 9 * * *", CalendarIngest.daily_tick)
```

## Late-arriving rule

Inside `GarminIngest.daily_tick` — exact code that wraps `should_fetch` from `pipeline-shared`. Same logic as today; trivial because handler code is plain Python.

## Backfill

No native UI. Two options:
- **CLI**: `restate invoke GarminIngest/fetch_window '{"start":"...","end":"..."}'`.
- **Custom UI**: small FastAPI page that lists invocations and lets you trigger backfills. Restate has an admin UI but it's "less polished than Dagit" (eval doc). For a homelab, the CLI is probably enough.

Resumability is automatic: if `fetch_window` crashes on day 45, restart it with the same ID and journal replay resumes from day 45.

## Failure surfacing

- **Retry policy** per `ctx.run` step — exponential, capped.
- **Permanent failures** after retries: handler errors out, becomes a "killed" invocation visible in admin UI. Failure handler (separate service `FailureHandler.on_invocation_failed`) writes a `notifications` row.
- **CAPTCHA/auth**: typed exception → caught at the handler level → writes critical notification → ntfy push. Operator resolves out-of-band; next scheduled tick picks up.

## Validation

`validate.py` checks become explicit steps inside `GarminIngest.fetch_day` after the upsert:
```python
check_result = await ctx.run("validate_sleep", lambda: validate_metric("sleep", date_str))
if not check_result.passed:
    await ctx.run("notify_validation", lambda: write_notification(...))
```

No per-asset check abstraction like Dagster. Validation results live in the `notifications` table or a dedicated `validation_results` table.

## Notifier

Either:
- A separate handler `Notifier.dispatch_pending` triggered after each notification insert via `service_send` (push model — preferred).
- Or a cron handler that polls every 30s (pull model — fallback).

## Docker / Caddy / Secrets

```yaml
# Restate server
restate-server:
  image: docker.restate.dev/restatedev/restate:latest
  volumes:
    - restate-data:/restate-data
  environment:
    RESTATE_NODE_NAME: pipeline-restate
  ports:
    - "9070"  # admin
  networks: [mybridge]

# SDK app (our handlers)
pipeline-restate:
  image: ankit/devbox:1.3
  working_dir: /workspace/pipeline-restate
  command: uv run python -m pipeline_restate.server
  env_file: ./secrets/pipeline-restate.env
  volumes: [.:/workspace]
  networks: [mybridge]
  depends_on: [restate-server, postgres]

volumes:
  restate-data:
```

Then register the SDK app: `curl restate-server:9070/deployments -d '{"uri":"http://pipeline-restate:9080"}'`. Done in a one-shot init container.

Caddy: `restate.home.ankitson.com → restate-server:9070` (admin UI).

`config/pipeline-restate.secrets.env.tmpl` — same as Dagster (GARMIN_*, DATABASE_URL, NTFY_*) plus `RESTATE_INGRESS=http://restate-server:8080`.

## Concrete shortcomings

1. **Hand-rolled partitioning**. No "asset is partitioned by date" concept — you write date loops yourself. Workable, but every new ETL source is more code than the Dagster equivalent.
2. **No backfill UI**. CLI-only is fine for one developer, painful with more.
3. **No asset lineage view**. You can see invocations but not "what assets depend on what."
4. **RocksDB state is opaque**. Recovery from corruption / backup story is "copy the RocksDB volume." Less ergonomic than DBOS's "it's all in your Postgres."
5. **Two containers** (server + SDK app) for what could be one in DBOS.
6. **BSL license** — fine for homelab use but worth noting.

## Verification

```bash
just up-restate
# Restate admin UI at https://restate.home.ankitson.com
# CLI: restate invoke GarminIngest/fetch_day '{"date_str":"2026-05-18"}'
# → confirm raw_responses + parsed tables populated
# Kill SDK app container mid-fetch → restart → confirm invocation resumes
# Trigger backfill: restate invoke GarminIngest/fetch_window '{"start":"...","end":"..."}'
# Inject anomaly → re-trigger Derived.refresh_day → confirm notification + ntfy
# Post test transaction > threshold → invoke BankAccount/apply_transaction
# → ntfy received → click approval URL → confirm awakeable resolved + tx committed
# Restart restate-server mid-pause → confirm awakeable still resolves correctly afterward
```
