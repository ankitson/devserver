"""Schema additions beyond what garmin-fetch's GarminStore creates.

When a GarminStore connects to a pipeline DB for the first time, it creates the
9 garmin tables (sleep, heart_rate, ..., raw_responses, fetch_log). We add:

- `pipeline_runs` — orchestrator-agnostic materialization log (replaces fetch_log
  for richer per-asset detail; fetch_log stays for backwards compat).
- `notifications` — alert/notification queue (anomaly events, bank approvals).
- `derived_daily` — daily cross-metric summary (sleep_score + hrv + readiness etc.).
- `rolling_7d` / `rolling_30d` — windowed aggregates.
- `anomaly_events` — detected anomalies per (date, metric, kind).
- `transactions` — banking transactions (mock bank).
- `bank_imports` — bank import workflow audit log.

Everything is created with `IF NOT EXISTS`. Idempotent.
"""

from __future__ import annotations

import psycopg

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           BIGSERIAL PRIMARY KEY,
    run_id       TEXT NOT NULL,
    tool         TEXT NOT NULL,
    asset        TEXT NOT NULL,
    partition_key TEXT,
    status       TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    duration_ms  INTEGER,
    error        TEXT,
    metadata     JSONB
);
CREATE INDEX IF NOT EXISTS pipeline_runs_asset_part ON pipeline_runs (asset, partition_key);
CREATE INDEX IF NOT EXISTS pipeline_runs_run ON pipeline_runs (run_id);

CREATE TABLE IF NOT EXISTS notifications (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'info',
    title        TEXT,
    body         TEXT,
    payload      JSONB,
    delivered_at TIMESTAMPTZ,
    delivered_via TEXT
);
CREATE INDEX IF NOT EXISTS notifications_undelivered
    ON notifications (created_at) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS derived_daily (
    date              DATE PRIMARY KEY,
    sleep_score       INTEGER,
    sleep_duration_h  REAL,
    hrv_last_night    REAL,
    hrv_weekly        REAL,
    resting_hr        INTEGER,
    stress_overall    INTEGER,
    body_battery_high INTEGER,
    body_battery_low  INTEGER,
    steps_total       INTEGER,
    readiness_score   REAL,
    readiness_level   TEXT,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rolling_7d (
    end_date          DATE PRIMARY KEY,
    sleep_score_avg   REAL,
    sleep_duration_h_avg REAL,
    hrv_avg           REAL,
    resting_hr_avg    REAL,
    stress_avg        REAL,
    steps_avg         REAL,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rolling_30d (
    end_date          DATE PRIMARY KEY,
    sleep_score_avg   REAL,
    sleep_score_stddev REAL,
    sleep_duration_h_avg REAL,
    hrv_avg           REAL,
    hrv_stddev        REAL,
    resting_hr_avg    REAL,
    resting_hr_stddev REAL,
    stress_avg        REAL,
    steps_avg         REAL,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id           BIGSERIAL PRIMARY KEY,
    date         DATE NOT NULL,
    metric       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    value        REAL,
    baseline     REAL,
    stddev       REAL,
    z_score      REAL,
    rule         TEXT,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (date, metric, kind, rule)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            BIGSERIAL PRIMARY KEY,
    external_id   TEXT UNIQUE NOT NULL,
    posted_date   DATE NOT NULL,
    amount_cents  BIGINT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    merchant      TEXT,
    category      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    approval_token TEXT,
    decided_at    TIMESTAMPTZ,
    decision      TEXT,
    raw_row       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS transactions_status ON transactions (status);

CREATE TABLE IF NOT EXISTS bank_imports (
    id           BIGSERIAL PRIMARY KEY,
    bank_name    TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running',
    statement_file TEXT,
    txn_count    INTEGER,
    error        TEXT
);

-- landing_zone batch ledger: one row per batch the file-drop sensor has seen.
-- Status flow: ready -> running -> done | failed.
CREATE TABLE IF NOT EXISTS landing_zone_batches (
    pipeline     TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    status       TEXT NOT NULL,
    ready_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    archived_to  TEXT,
    files        JSONB,
    file_count   INTEGER,
    row_count    INTEGER,
    error        TEXT,
    PRIMARY KEY (pipeline, batch_id)
);

-- Playnite GameActivity: one row per (game, session-start). Re-importing the
-- same batch is idempotent.
CREATE TABLE IF NOT EXISTS playnite_sessions (
    game_id          TEXT NOT NULL,
    game_name        TEXT NOT NULL,
    date_session     TIMESTAMPTZ NOT NULL,
    elapsed_seconds  BIGINT NOT NULL,
    source_id        TEXT,
    source_name      TEXT,
    platform_ids     TEXT[],
    platform_names   TEXT[],
    game_action_name TEXT,
    id_configuration INTEGER,
    raw              JSONB,
    imported_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_id         TEXT,
    PRIMARY KEY (game_id, date_session)
);
CREATE INDEX IF NOT EXISTS playnite_sessions_date
    ON playnite_sessions (date_session);
CREATE INDEX IF NOT EXISTS playnite_sessions_source
    ON playnite_sessions (source_name);
"""


def ensure_schema(database_url: str) -> None:
    """Create extra pipeline tables if missing. Garmin tables are created by
    GarminStore on its own first connection."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
