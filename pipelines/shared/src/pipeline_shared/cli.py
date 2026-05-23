"""Small CLI for setup, seeding, and inspection. Used by Justfile and during tests.

Subcommands:
  init                            ensure_schema on the target DB
  seed [--start D --end D]        copy raw_responses from source garmin DB
  reparse <date>                  reparse all 7 metrics for a single day
  reparse-range <start> <end>     reparse for a date range
  derive <date>                   compute derived_daily + rolling for one date
  detect <date>                   run anomaly detection for one date
  notify                          drain the notifications queue
  dates                           list distinct dates in source raw_responses
  status                          show row counts per table in the target DB
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import date, timedelta

import psycopg

from pipeline_shared import (
    GarminRun,
    METRIC_NAMES,
    Notifier,
    detect_anomalies_for_day,
    ensure_schema,
    load_settings,
    refresh_derived_for_day,
    reparse_day,
    seed_raw_responses_from_garmin,
)
from pipeline_shared.seed import list_available_dates


def _make_run(tool: str) -> GarminRun:
    s = load_settings()
    return GarminRun(
        settings=s,
        target_url=s.database_url,
        source_url=s.garmin_source_database_url,
        tool=tool,
        run_id=str(uuid.uuid4()),
    )


def cmd_init(args: argparse.Namespace) -> int:
    s = load_settings()
    from garmin_fetch.store import GarminStore
    GarminStore(s.database_url).close()  # creates 9 garmin tables
    ensure_schema(s.database_url)        # creates extra pipeline tables
    print("schema ready on", s.database_url)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    s = load_settings()
    n = seed_raw_responses_from_garmin(
        source_url=s.garmin_source_database_url,
        target_url=s.database_url,
        start=args.start, end=args.end, metrics=args.metric,
    )
    print(f"seeded {n} raw_responses rows")
    return 0


def cmd_reparse(args: argparse.Namespace) -> int:
    run = _make_run(tool=args.tool)
    results = reparse_day(run, args.date)
    for r in results:
        print(f"{args.date}\t{r.metric:24}\t{r.status:10}\tparsed={r.parsed_rows}")
    return 0


def cmd_reparse_range(args: argparse.Namespace) -> int:
    run = _make_run(tool=args.tool)
    d = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    total = 0
    while d <= end:
        results = reparse_day(run, d.isoformat())
        ok = sum(1 for r in results if r.status == "ok")
        skipped = sum(1 for r in results if r.status in ("skipped", "no_data", "no_raw"))
        errored = sum(1 for r in results if r.status.startswith("error"))
        total += ok
        print(f"{d.isoformat()}  ok={ok}  skipped={skipped}  error={errored}")
        d += timedelta(days=1)
    print(f"\ntotal ok: {total}")
    return 0


def cmd_derive(args: argparse.Namespace) -> int:
    s = load_settings()
    counts = refresh_derived_for_day(s.database_url, args.date)
    for k, v in counts.items():
        print(f"{k}: {v}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    s = load_settings()
    dets = detect_anomalies_for_day(s.database_url, args.date)
    print(json.dumps(dets, indent=2, default=str))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    s = load_settings()
    n = Notifier(s)
    pushed = n.drain_once(push=args.push)
    print(f"pushed {pushed} notification(s)")
    return 0


def cmd_dates(args: argparse.Namespace) -> int:
    s = load_settings()
    dates = list_available_dates(s.garmin_source_database_url)
    print(f"{len(dates)} dates between {dates[0]} and {dates[-1]}" if dates else "no dates")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    s = load_settings()
    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        tables = [
            "raw_responses", "sleep", "heart_rate", "hrv", "stress",
            "body_battery", "steps", "training_readiness",
            "heart_rate_samples",
            "derived_daily", "rolling_7d", "rolling_30d",
            "anomaly_events", "notifications", "transactions", "bank_imports",
            "pipeline_runs",
        ]
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*), MIN(date), MAX(date) FROM {t}")
                row = cur.fetchone()
                print(f"{t:24} count={row[0]:>6}  range={row[1]}..{row[2]}")
            except psycopg.errors.UndefinedColumn:
                # tables without `date` column
                conn.rollback()
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                print(f"{t:24} count={cur.fetchone()[0]}")
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                print(f"{t:24} (missing)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline-shared")
    p.add_argument("--tool", default="shared",
                   help="tool name recorded in pipeline_runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    sp = sub.add_parser("seed")
    sp.add_argument("--start"); sp.add_argument("--end")
    sp.add_argument("--metric", action="append")

    sp = sub.add_parser("reparse"); sp.add_argument("date")
    sp = sub.add_parser("reparse-range"); sp.add_argument("start"); sp.add_argument("end")
    sp = sub.add_parser("derive"); sp.add_argument("date")
    sp = sub.add_parser("detect"); sp.add_argument("date")
    sp = sub.add_parser("notify"); sp.add_argument("--push", action="store_true", default=True)
    sub.add_parser("dates")
    sub.add_parser("status")

    return p


HANDLERS = {
    "init": cmd_init,
    "seed": cmd_seed,
    "reparse": cmd_reparse,
    "reparse-range": cmd_reparse_range,
    "derive": cmd_derive,
    "detect": cmd_detect,
    "notify": cmd_notify,
    "dates": cmd_dates,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return HANDLERS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
