"""Seed helpers.

Copy raw_responses from the canonical garmin DB into a pipeline's own DB so
the pipeline can run reparse-only workflows without ever touching the
external Garmin API.

The source/target databases are addressed by URL — they may be the same
database or different DBs on the same Postgres instance.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import psycopg

log = logging.getLogger(__name__)


def seed_raw_responses_from_garmin(
    *,
    source_url: str,
    target_url: str,
    start: str | None = None,
    end: str | None = None,
    metrics: list[str] | None = None,
) -> int:
    """Copy raw_responses rows from source → target. Returns count copied.

    Filters by date range and metric list (all by default). Existing target rows
    are updated (raw_responses upsert is by (date, metric)).
    """
    where_clauses: list[str] = []
    params: list = []
    if start is not None:
        where_clauses.append("date >= %s")
        params.append(start)
    if end is not None:
        where_clauses.append("date <= %s")
        params.append(end)
    if metrics:
        where_clauses.append("metric = ANY(%s)")
        params.append(list(metrics))
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with psycopg.connect(source_url) as src, psycopg.connect(target_url) as dst:
        with src.cursor() as scur, dst.cursor() as dcur:
            scur.execute(
                f"SELECT date, metric, response, fetched_at FROM raw_responses {where}",
                params,
            )
            n = 0
            for date_val, metric, response, fetched_at in scur:
                dcur.execute(
                    """
                    INSERT INTO raw_responses (date, metric, response, fetched_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (date, metric) DO UPDATE
                    SET response = EXCLUDED.response, fetched_at = EXCLUDED.fetched_at
                    """,
                    (date_val, metric, response, fetched_at),
                )
                n += 1
            dst.commit()
    log.info("seeded %d raw_responses rows", n)
    return n


def list_available_dates(source_url: str) -> list[date]:
    """Return distinct dates present in source raw_responses, ascending."""
    with psycopg.connect(source_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT date FROM raw_responses ORDER BY date"
            )
            return [row[0] for row in cur.fetchall()]
