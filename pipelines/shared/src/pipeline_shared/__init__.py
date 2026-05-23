"""Shared library for Garmin/banking data pipelines.

Wraps the existing `garmin-fetch` package as a library and adds:
- run-scoped Garmin fetch helpers (one login, rate-limited, idempotent)
- extra schema (notifications, transactions, derived tables)
- derived-layer SQL (rolling aggregates, cross-metric joins)
- rule-based anomaly detection
- notifier (notifications table + ntfy publisher)
- banking primitives (mock-bank client, Playwright runner, OTP awakeable protocol)
- seed helpers (copy raw_responses from source garmin DB)

Each orchestrator (Dagster, DBOS, Restate) imports from here. No orchestrator
code lives in this package — only domain logic.
"""

from pipeline_shared.config import Settings, load_settings
from pipeline_shared.garmin import (
    METRIC_NAMES,
    GarminRun,
    fetch_metric,
    reparse_metric,
    reparse_day,
)
from pipeline_shared.schema import ensure_schema
from pipeline_shared.notifier import Notifier
from pipeline_shared.derived import refresh_derived_for_day
from pipeline_shared.anomaly import detect_anomalies_for_day
from pipeline_shared.banking import (
    AsyncOtpAwaiter,
    BankImportRequest,
    OtpRequest,
    OtpAwaiter,
    bank_login_and_pause,
    bank_resume_and_download,
    run_bank_import,
    run_bank_import_async,
    process_statement_csv,
)
from pipeline_shared.gameactivity import import_batch as gameactivity_import_batch
from pipeline_shared.gameactivity import parse_game_file as gameactivity_parse_game_file
from pipeline_shared.seed import seed_raw_responses_from_garmin
__all__ = [
    "Settings",
    "load_settings",
    "METRIC_NAMES",
    "GarminRun",
    "fetch_metric",
    "reparse_metric",
    "reparse_day",
    "ensure_schema",
    "Notifier",
    "refresh_derived_for_day",
    "detect_anomalies_for_day",
    "AsyncOtpAwaiter",
    "BankImportRequest",
    "OtpRequest",
    "OtpAwaiter",
    "bank_login_and_pause",
    "bank_resume_and_download",
    "run_bank_import",
    "run_bank_import_async",
    "process_statement_csv",
    "seed_raw_responses_from_garmin",
    "gameactivity_import_batch",
    "gameactivity_parse_game_file",
]
