"""Playnite GameActivity ingestion.

Each `.json` file is one Playnite GameActivity export — a single game with a
list of play sessions:

  {
    "GameId": "0dfc3d26-...",
    "GameName": "Age of Empires IV",
    "Activity": {
      "SessionPlaytime": 701585,
      "Items": [
        {"DateSession": "2025-07-22T04:30:57Z", "ElapsedSeconds": 2731,
         "SourceID": "...", "SourceName": "Steam", ...},
        ...
      ]
    }
  }

A "batch" is a directory of these JSONs dropped into landing_zone/. The
upserter writes one row per (game_id, date_session) into `playnite_sessions`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg

log = logging.getLogger(__name__)


class NotAGameActivityFile(ValueError):
    """Raised when a JSON file in a batch is valid JSON but not a Playnite
    GameActivity export (e.g. the exporter's `state.json` manifest). The batch
    importer treats this as "skip this file", not a batch failure. Malformed
    JSON raises json.JSONDecodeError instead and DOES fail the batch."""


def parse_game_file(path: Path) -> Iterator[dict[str, Any]]:
    """Parse one Playnite GameActivity JSON file into session rows.

    Yields one row per Activity.Items[] entry. A file with no sessions yields
    nothing (no error).
    """
    with path.open("r", encoding="utf-8-sig") as f:
        doc = json.load(f)
    game_id = doc.get("GameId")
    game_name = doc.get("GameName") or "<unknown>"
    if not game_id:
        raise NotAGameActivityFile(f"{path.name}: missing GameId")
    items = ((doc.get("Activity") or {}).get("Items") or [])
    for item in items:
        date_session = item.get("DateSession")
        if not date_session:
            continue
        yield {
            "game_id": game_id,
            "game_name": game_name,
            "date_session": date_session,
            "elapsed_seconds": int(item.get("ElapsedSeconds") or 0),
            "source_id": item.get("SourceID"),
            "source_name": item.get("SourceName"),
            "platform_ids": item.get("PlatformIDs") or [],
            "platform_names": item.get("PlatformNames") or [],
            "game_action_name": item.get("GameActionName"),
            "id_configuration": item.get("IdConfiguration"),
            "raw": item,
        }


_UPSERT_SQL = """
INSERT INTO playnite_sessions (
    game_id, game_name, date_session, elapsed_seconds,
    source_id, source_name, platform_ids, platform_names,
    game_action_name, id_configuration, raw, batch_id
) VALUES (
    %(game_id)s, %(game_name)s, %(date_session)s, %(elapsed_seconds)s,
    %(source_id)s, %(source_name)s, %(platform_ids)s, %(platform_names)s,
    %(game_action_name)s, %(id_configuration)s, %(raw)s, %(batch_id)s
)
ON CONFLICT (game_id, date_session) DO UPDATE SET
    game_name        = EXCLUDED.game_name,
    elapsed_seconds  = EXCLUDED.elapsed_seconds,
    source_id        = EXCLUDED.source_id,
    source_name      = EXCLUDED.source_name,
    platform_ids     = EXCLUDED.platform_ids,
    platform_names   = EXCLUDED.platform_names,
    game_action_name = EXCLUDED.game_action_name,
    id_configuration = EXCLUDED.id_configuration,
    raw              = EXCLUDED.raw,
    batch_id         = EXCLUDED.batch_id,
    imported_at      = NOW()
"""


def import_batch(
    *,
    database_url: str,
    batch_dir: Path,
    batch_id: str,
) -> dict[str, int]:
    """Parse every *.json in `batch_dir` and upsert all sessions in one tx.

    Returns counts: {"files": N, "rows": M, "games": G, "skipped": S}.
    Files that are valid JSON but not GameActivity exports (e.g. the exporter's
    state.json manifest) are skipped, not fatal. Malformed JSON DOES fail the
    batch — caller (the Dagster job) handles archive / status update.
    """
    json_files = sorted(p for p in batch_dir.iterdir()
                        if p.is_file() and p.suffix == ".json"
                        and not p.name.startswith("_")
                        and not p.name.startswith(".")
                        and not p.name.endswith(".tmp"))
    if not json_files:
        raise RuntimeError(f"no *.json files in {batch_dir}")

    rows: list[dict[str, Any]] = []
    games: set[str] = set()
    skipped: list[str] = []
    for jf in json_files:
        try:
            file_rows = list(parse_game_file(jf))
        except NotAGameActivityFile:
            skipped.append(jf.name)
            continue
        for row in file_rows:
            row["batch_id"] = batch_id
            row["raw"] = json.dumps(row["raw"])
            rows.append(row)
            games.add(row["game_id"])

    if skipped:
        log.info("batch %s skipped %d non-game file(s): %s",
                 batch_id, len(skipped), ", ".join(skipped[:5]))
    if not rows:
        log.warning("batch %s parsed %d files but produced 0 session rows",
                    batch_id, len(json_files))

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            if rows:
                cur.executemany(_UPSERT_SQL, rows)
        conn.commit()

    return {"files": len(json_files), "rows": len(rows),
            "games": len(games), "skipped": len(skipped)}
