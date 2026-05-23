# General Data Pipeline Plan (Tool-Agnostic)

This is the general architectural plan. Three tool-specific plans (Dagster, Restate, DBOS) will each be a concrete realization of the contract defined here.

## Context

`garmin-fetch` already works: a Python CLI that pulls 7 Garmin metrics → parses → writes to Postgres, run via cron in Docker on the homeserver. The 2026-03-10 Dagster eval concluded that for the current narrow use case (7 metrics, 3x/day, one user) any orchestrator is overkill — but flagged that:

- Backfill experience, observability, and asset lineage are real wins **as the system grows**
- A second, durable-execution-shaped workload (banking flows with human approval) is on the roadmap and won't fit an orchestrator
- The existing code is already structured asset-like (raw → parsed, idempotent upserts, reparse-from-cache), so swapping the orchestration layer is the cheap part

This project intentionally builds out the same workload (Garmin + Calendar + Banking) on **three tools side-by-side on the devserver**, to compare them in practice rather than on paper. This document defines what each implementation must do; the per-tool plans define how each one does it.

## Goals

1. **Garmin (asset-graph ETL)**: feature-parity with current `garmin-fetch`, plus derived/analytical layer and anomaly alerts.
2. **Calendar (asset-graph ETL)**: ingest calendar events so they can join Garmin metrics (meeting load × HRV, etc.).
3. **Banking (durable workflow)**: long-running flow with human approval (e.g., categorize large transactions, block on user approval, then commit). Tests the durable-execution side.
4. **Deploy all three tools** on the devserver as parallel experiments to gather a real comparison.

## Non-goals

- No production homeserver rollout in this plan. Devserver only. Promotion decided later based on what we learn.
- No replacement of `garmin-fetch`'s parsing/store code — those stay as a library. Only the orchestration shell changes.
- No multi-user, no auth on UIs beyond the existing Caddy/Tailscale boundary.
- No new database engine — reuse Postgres (existing `garmin` DB on homeserver pattern; devserver gets its own DB instance or DB-on-DB).

## Two Coexisting Shapes

The pipeline has two structurally different workloads that the same orchestrator (or two cooperating ones) must serve:

| Shape | Workload | Primary abstraction |
|---|---|---|
| **Asset-graph ETL** | Garmin daily fetch, Calendar daily fetch, derived rollups, anomaly detection | Date-partitioned assets with lineage |
| **Durable workflow** | Banking: pull tx → categorize → if large, wait for human approval → commit | Code-as-workflow with `awakeable`/`recv`/wait-for-event |

Dagster is strong on the first, weak on the second. Restate/DBOS are strong on the second, hand-rolled on the first. The per-tool plans will be honest about where each tool fits naturally vs where it's stretched.

## Sources (this plan)

### 1. Garmin (existing — wrapped, not rewritten)

Already mapped in `garmin-fetch`. Keep as a library:
- `GarminClient` (auth, retry, rate-limit) — `client.py:19-59`
- `GarminStore` (schema, upserts, `should_fetch`) — `store.py:434-456` (should_fetch), `store.py:151-326` (upserts)
- Parser functions — `fetcher.py:192-205` (FETCHERS dict)

Orchestration layer replaces: `fetcher.fetch_day`, `fetcher.fetch_range`, `cli.py` commands.

7 metrics, daily-partitioned: sleep, heart_rate, hrv, stress, body_battery, steps, training_readiness. Plus `stats` (shared input to 3 metrics) and `heart_rate_samples` (intraday).

### 2. Calendar (new)

Pull calendar events daily, store as `calendar_events` rows (date, start, end, title, attendees, meeting_type). Source TBD per implementation (Google Calendar API or CalDAV via Keeper which already syncs calendar data). Same daily-partitioned shape.

### 3. Banking (new, durable-workflow shape)

Each transaction is a workflow instance, not a daily asset. Sketch:
```
on_new_transaction(tx):
    categorized = categorize(tx)          # step
    if tx.amount > THRESHOLD:
        token = ctx.awakeable()           # durable pause
        send_push(approval_link(token))   # ntfy push w/ approve/reject URL
        decision = await token            # may block for hours/days
        if not decision.approved:
            mark_rejected(tx); return
    commit_transaction(tx, categorized)
```

Transaction source: TBD (Plaid/Akoya/manual CSV import — each tool's plan will pick one for the prototype). Storage: `transactions` table + `workflow_state` (durable-execution tool's domain).

## Data Layers

A single Postgres schema (per source-namespace) with these tiers. Idempotency is required at every tier.

```
L0  External source (Garmin API, Calendar provider, Bank API)
       ↓
L1  RAW              raw_responses(date, source, metric, response JSONB, fetched_at)
       ↓             — write-once-per-fetch cache; lets L2 reparse without API calls
L2  PARSED           sleep / heart_rate / hrv / ... / calendar_events / transactions
       ↓             — typed columns, ON CONFLICT(date,...) DO UPDATE
L3  DERIVED          daily_summary, rolling_7d, rolling_30d, sleep_hrv_corr,
       ↓             meeting_load_vs_hrv, training_readiness_trend, ...
L4  SINKS            notifications (alerts table), ntfy push, materialized views
                     — also: derived views are themselves the "exports" per the user's choice
```

**L1 (Raw) acts as the durability boundary.** Once the API response lands in `raw_responses`, the rest of the pipeline (parse → derive → alert) can re-run without any external dependency. Reparse and backfill of derived layers become free.

**L2 (Parsed)** is the existing per-metric upserts. New sources follow the same pattern.

**L3 (Derived)** is new. Examples (the user picked all four processing types):

- **Rolling aggregates**: `rolling_7d_metrics`, `rolling_30d_metrics` — windowed averages of sleep score, HRV, resting HR, stress, body battery.
- **Cross-metric / cross-source correlations**:
  - `sleep_hrv_daily` — sleep score, HRV, training readiness on one row per day
  - `meeting_load_vs_recovery` — calendar meeting count/duration joined with HRV and stress
  - `activity_vs_sleep` — workouts (from activities) vs next-night sleep score
- **Anomaly detection**: `anomaly_candidates(date, metric, kind, severity, value, baseline)` produced by simple rules (z-score over 30-day baseline, threshold drops). Rules first, ML later.

**L4 (Sinks)**:
- `notifications(id, created_at, kind, severity, payload, delivered_at, delivered_via)` — anomalies write here.
- ntfy push for any `notifications` row not yet delivered (or for severity ≥ N) — published to a private ntfy topic, opened on phone.
- Materialized views in shared Postgres = the "exports" (other homeserver tools can read these directly; no separate file dump needed).

## Scheduling & Freshness

| Job | Cadence | Inputs |
|---|---|---|
| Garmin daily fetch | 3x/day (08:00, 14:00, 22:00 UTC) — same as today | today + yesterday partitions, all 7 metrics + stats + activities |
| Calendar daily fetch | 1x/day (morning) | trailing 7 days (events get edited; refetch a window) |
| Derived rollups (L3) | Triggered downstream of L2 materializations | trailing 30 days |
| Anomaly detection | Triggered downstream of L3 | latest 1 day |
| Banking transaction ingest | Event-driven (webhook) or 1x/day poll | new transactions only |
| Banking approval workflow | Per-transaction, durable | indefinite (waits for human) |

**Late-arriving data rule (Garmin specifics)** — keep current behavior, codified once:
- `today` partition: always re-fetch on every run (data accumulates throughout the day)
- `yesterday` partition: re-fetch only if last fetch was before 10:00 UTC (Garmin finalizes sleep ~6 AM next day)
- older partitions: skip if data present (frozen)

The per-tool plans must implement this rule. Three options were considered in the eval (always-refetch / sensor / inside-asset); the per-tool plans pick.

## Reliability Contract

Every implementation must satisfy these:

1. **Idempotent upserts**: re-running any L1/L2/L3 step for the same partition is safe — same input → same row. (Already true in `garmin-fetch`; new code preserves this.)
2. **Per-partition retry with backoff**: when a single date for a single metric fails, only that one retries — no full-run restart. Backoff: exponential, jittered, capped (e.g., 1s → 30s).
3. **Run-scoped Garmin auth**: one `GarminClient.login()` shared across all metrics in a run. Avoids 7-8 logins per run + CAPTCHA risk.
4. **Rate limit**: ≥1s between Garmin API calls (already in `client.py:_rate_limit`). Orchestrator must not parallelize Garmin calls inside one auth session.
5. **API failure surfaces**:
   - 429 (rate limit) → 30s backoff, retry (existing behavior in `client.py:19-59`)
   - 401/403 (auth) → re-login once, then fail run with explicit "needs human" status
   - CAPTCHA/2FA prompt → run fails with notification; human resolves out-of-band; next run picks up
6. **Backfill resumability**: a 90-day backfill that fails on day 45 resumes from day 45, not day 1.
7. **Banking workflow durability**: approval-pending workflows survive orchestrator restarts and stay paused for hours/days/weeks without holding a live process.

## Consistency Contract

**Per-metric idempotent (status quo)** — confirmed with the user. Each metric's upsert commits independently. Partial-day success is acceptable: if sleep lands but HRV fails, sleep is kept and HRV retries on next run. This matches existing `garmin-fetch` behavior (`store.py` commits inside each upsert method).

Implication for tool choice: we do NOT need transactional cross-asset writes. Tools like DBOS that offer first-class DB transactions get partial credit, but it's not a hard requirement for the ETL side. For the banking workflow side, transactional commit of "step + workflow checkpoint" IS desirable (no "money moved but workflow forgot").

## Observability Contract

Every implementation must expose:

1. **Materialization log**: for each (partition, asset, run) — status, duration, error, code version. Replaces `fetch_log`.
2. **Per-asset validation results**: surfaces the checks currently in `validate.py:7-43` per-asset, not as a separate run. Visible in UI / queryable.
3. **Run history**: filterable by date, source, status. Last N runs trivially viewable.
4. **Health endpoint** or UI accessible via Caddy at `<tool>.home.ankitson.com` (each tool gets a subdomain; UI is the easy win for all three).

## Notifications

Single internal contract:

```
INSERT INTO notifications(kind, severity, payload, created_at, delivered_at, delivered_via)
       VALUES (...)
```

A small "notifier" worker (any tool) reads new rows and dispatches:
- All rows: visible in a future homelab dashboard
- `severity >= warn`: ntfy push to private topic → phone

This is intentionally tool-agnostic — same `notifications` table whether produced by Dagster sensors, DBOS event listeners, or Restate handlers.

## Deployment Shape (devserver)

Three parallel implementations under the devserver, each independently runnable:

```
/home/ankit/hroot/devserver/
  docker-compose.yml          (existing — adds 3 new service groups)
  config/                     (existing — *.env.tmpl, op inject)
  pipeline-dagster/           (new — service definitions, dagster project)
  pipeline-restate/           (new — service definitions, restate handlers)
  pipeline-dbos/              (new — service definitions, DBOS python project)
  pipeline-shared/            (new — library: garmin client/store/parsers,
                                          calendar client, banking sources,
                                          derived-layer SQL, anomaly rules,
                                          notifier worker — reused by all three)
```

Critical principle: **`pipeline-shared/` holds the source-of-truth domain code.** Each tool's directory is *only* the orchestration shell — schedules, asset definitions, workflow declarations, retries config. Comparing tools means comparing the shell, not three forks of the parsing/store code.

`pipeline-shared/` reuses (does not rewrite):
- `garmin-fetch/garmin_fetch/client.py`, `store.py`, `validate.py`, parsers in `fetcher.py`
- These are imported as a library (vendored or installed via `uv pip install -e ../../projects/garmin-fetch`)

Each tool's compose service group needs:
- Container(s) for the tool itself (Dagster needs 2-3; DBOS rides in-process; Restate is 1 binary)
- A Postgres database (separate DB per tool, single shared Postgres instance — already running on devserver via `mybridge` network)
- Caddy route to admin UI: `dagster.home.ankitson.com`, `restate.home.ankitson.com`, `dbos.home.ankitson.com`
- Secrets template: `config/pipeline-<tool>.secrets.env.tmpl` with 1Password references for `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `DATABASE_URL`, calendar creds, banking creds, ntfy token

## What Each Tool-Specific Plan Must Answer

Each follow-up plan (Dagster, Restate, DBOS) will spell out, concretely:

1. **Asset/job/workflow definitions** for all 7 Garmin metrics + Calendar + L3 derived + anomaly detection.
2. **Banking durable workflow** with awakeable/recv-style approval gate.
3. **Schedule definitions** (3x/day Garmin, 1x/day Calendar, downstream derived).
4. **Late-arriving rule implementation** (which of the three approaches).
5. **Backfill story** (UI? CLI? per-partition retry?).
6. **Failure surfacing** (CAPTCHA, auth, rate limit) — how does it become visible/actionable?
7. **Validation integration** (validate.py logic → asset checks / step assertions).
8. **Docker compose entries**, env files, Caddy routes.
9. **Concrete shortcomings** for this workload — be honest where the tool is a stretch.
10. **Verification recipe** — how to spin it up, trigger a fetch, see results, simulate a banking approval.

## Critical Files

These are the existing files each tool's plan will need to reference / wire into:

- `/home/ankit/hroot/projects/garmin-fetch/garmin_fetch/client.py` — wrap, run-scope
- `/home/ankit/hroot/projects/garmin-fetch/garmin_fetch/store.py` — reuse upserts, schema, `should_fetch`
- `/home/ankit/hroot/projects/garmin-fetch/garmin_fetch/fetcher.py` — reuse parsers, replace `fetch_day`/`fetch_range`
- `/home/ankit/hroot/projects/garmin-fetch/garmin_fetch/validate.py` — port checks to per-asset
- `/home/ankit/hroot/devserver/docker-compose.yml` — add three service groups
- `/home/ankit/hroot/devserver/Justfile` — add `just up-dagster`, `just up-restate`, `just up-dbos`, etc.
- `/home/ankit/hroot/devserver/config/` — add `pipeline-*.secrets.env.tmpl` templates
- `/home/ankit/hroot/homeserver/volumes/caddy/Caddyfile` (or devserver equivalent) — add 3 admin-UI routes

New files (in this devserver repo):
- `/home/ankit/hroot/devserver/pipeline-shared/` — domain library reused by all three
- `/home/ankit/hroot/devserver/pipeline-dagster/` — Dagster project
- `/home/ankit/hroot/devserver/pipeline-restate/` — Restate handlers
- `/home/ankit/hroot/devserver/pipeline-dbos/` — DBOS project

## Verification (applies to each implementation)

End-to-end smoke test the per-tool plan must support:

1. `just up-<tool>` brings the service group up; Caddy routes resolve; UI loads.
2. Trigger one Garmin daily run for today + yesterday → confirm 7 metric tables + `raw_responses` populated for those dates.
3. Trigger a backfill for last 7 days → confirm progress visible in UI, all dates land, restart-from-failure works (test by killing the worker mid-run).
4. Confirm L3 derived tables (`rolling_7d_metrics`, `sleep_hrv_daily`, etc.) populate downstream.
5. Inject an anomaly (manually edit a sleep_score to 0) → confirm `notifications` row created → confirm ntfy push received on phone.
6. Run Calendar fetch → confirm `calendar_events` populated → confirm `meeting_load_vs_recovery` joins it with HRV.
7. Submit a banking transaction above threshold → confirm push received with approval link → click approve → confirm transaction commits and `notifications` records the decision. Restart the orchestrator mid-pause and confirm workflow resumes correctly.
8. Run all three tools simultaneously against their own DBs and compare: UI ergonomics, observability, backfill flow, banking workflow fluency, resource footprint (`docker stats`).

## Open questions for tool-specific plans

These intentionally stay open until each tool's plan answers them in its own terms:

- Calendar source choice (Google Calendar API direct vs read from Keeper's Postgres) — may differ per tool plan.
- Banking source choice (Plaid, Akoya, manual CSV ingest) for the prototype.
- Anomaly detection: rules-only for v1, or include simple z-score from baseline? (Default: rules + 30-day z-score.)
- Notifier worker — separate microservice, or embedded as a sensor/scheduled job per tool?
