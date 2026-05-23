from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Environment-driven settings shared across pipelines.

    DATABASE_URL — pipeline's own DB (parsed/derived/notifications/transactions).
    GARMIN_SOURCE_DATABASE_URL — read-only access to the canonical garmin DB
        (where raw_responses lives, accumulated by the existing garmin-fetch
        cron job). Used for reparse/seed. Optional; falls back to DATABASE_URL.
    GARMIN_EMAIL / GARMIN_PASSWORD — only needed if live fetching enabled.
    GARMIN_LIVE_FETCH — bool, default False. When False, all "fetch" operations
        are forced to read from raw_responses cache instead of hitting the API.
        Conservative default: pipelines never hit Garmin unless explicitly opted in.
    GARMIN_MIN_REQUEST_INTERVAL_SECONDS — minimum spacing between Garmin API
        calls (when GARMIN_LIVE_FETCH=True). Default 2.0 — twice the floor in
        garmin-fetch's own client.
    NTFY_URL / NTFY_TOPIC / NTFY_TOKEN — push notifications.
    MOCK_BANK_URL — local mock bank service.
    HEADLESS_BROWSER — bool, default True.
    """

    database_url: str
    garmin_source_database_url: str
    garmin_email: str | None
    garmin_password: str | None
    garmin_live_fetch: bool
    garmin_min_request_interval_seconds: float
    ntfy_url: str
    ntfy_topic: str | None
    ntfy_token: str | None
    mock_bank_url: str
    headless_browser: bool

    def has_garmin_creds(self) -> bool:
        return bool(self.garmin_email and self.garmin_password)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> Settings:
    db = os.environ.get("DATABASE_URL")
    if not db:
        raise RuntimeError("DATABASE_URL is required")
    return Settings(
        database_url=db,
        garmin_source_database_url=os.environ.get("GARMIN_SOURCE_DATABASE_URL", db),
        garmin_email=os.environ.get("GARMIN_EMAIL"),
        garmin_password=os.environ.get("GARMIN_PASSWORD"),
        garmin_live_fetch=_bool_env("GARMIN_LIVE_FETCH", False),
        garmin_min_request_interval_seconds=float(
            os.environ.get("GARMIN_MIN_REQUEST_INTERVAL_SECONDS", "2.0")
        ),
        ntfy_url=os.environ.get("NTFY_URL", "https://ntfy.sh"),
        ntfy_topic=os.environ.get("NTFY_TOPIC"),
        ntfy_token=os.environ.get("NTFY_TOKEN"),
        mock_bank_url=os.environ.get("MOCK_BANK_URL", "http://mock-bank:8000"),
        headless_browser=_bool_env("HEADLESS_BROWSER", True),
    )
