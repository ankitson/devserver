"""Notifier — reads the notifications table and dispatches.

Two sinks:
  - the table itself (always written; query for history)
  - ntfy push for severity in {warn, critical} (or all, configurable)

`drain_once()` is the unit each orchestrator can call from a scheduled task /
sensor. It is safe to call concurrently — uses SELECT ... FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import json
import logging

import httpx
import psycopg

from pipeline_shared.config import Settings

log = logging.getLogger(__name__)

_PUSH_SEVERITIES = {"warn", "critical"}


class Notifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    def enqueue(
        self,
        *,
        kind: str,
        severity: str = "info",
        title: str | None = None,
        body: str | None = None,
        payload: dict | None = None,
    ) -> int:
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notifications (kind, severity, title, body, payload)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (kind, severity, title, body, json.dumps(payload) if payload else None),
                )
                return cur.fetchone()[0]

    def drain_once(self, *, push: bool = True, max_rows: int = 50) -> int:
        """Pop pending notifications, dispatch (if push and topic configured),
        mark delivered. Returns the count actually pushed. Always safe to call.
        """
        pushed = 0
        with psycopg.connect(self.settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, kind, severity, title, body, payload
                    FROM notifications
                    WHERE delivered_at IS NULL
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (max_rows,),
                )
                rows = cur.fetchall()
                for row in rows:
                    delivered_via = "table"
                    if push and row["severity"] in _PUSH_SEVERITIES:
                        if self._push_to_ntfy(row):
                            delivered_via = "ntfy"
                            pushed += 1
                    cur.execute(
                        "UPDATE notifications SET delivered_at = NOW(), delivered_via = %s "
                        "WHERE id = %s",
                        (delivered_via, row["id"]),
                    )
                conn.commit()
        return pushed

    def _push_to_ntfy(self, row: dict) -> bool:
        if not self.settings.ntfy_topic:
            return False
        url = f"{self.settings.ntfy_url.rstrip('/')}/{self.settings.ntfy_topic}"
        title = row["title"] or row["kind"]
        body = row["body"] or json.dumps(row.get("payload") or {})
        headers = {
            "Title": title,
            "Priority": "high" if row["severity"] == "critical" else "default",
            "Tags": row["kind"],
        }
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        try:
            r = httpx.post(url, data=body, headers=headers, timeout=10.0)
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy push failed: %s", e)
            return False
