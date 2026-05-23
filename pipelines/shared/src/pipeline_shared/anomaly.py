"""Rule-based anomaly detection.

Reads derived_daily for the target date and rolling_30d for the previous day's
baseline, applies a small ruleset, writes detections to anomaly_events, and
mirrors warn+ severities into the notifications table.

Rules:
  - sleep_score < 50            → severity=warn,  kind=low_sleep_score
  - resting_hr z > 2.5          → severity=warn,  kind=resting_hr_spike
  - hrv_last_night z < -2.5     → severity=warn,  kind=hrv_drop
  - sleep_duration_h < 4.0      → severity=critical, kind=very_short_sleep
  - steps_total == 0 AND date<today  → severity=info, kind=zero_steps

Idempotent via UNIQUE(date, metric, kind, rule) on anomaly_events.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, timedelta

import psycopg

log = logging.getLogger(__name__)


@dataclass
class Detection:
    date: str
    metric: str
    kind: str
    severity: str
    rule: str
    value: float | None = None
    baseline: float | None = None
    stddev: float | None = None
    z_score: float | None = None


def _zscore(value: float | None, mean: float | None, stddev: float | None) -> float | None:
    if value is None or mean is None or stddev is None or stddev == 0:
        return None
    return (value - mean) / stddev


def _detect(today_row: dict, baseline_row: dict | None) -> list[Detection]:
    out: list[Detection] = []
    date_str = today_row["date"].isoformat() if hasattr(today_row["date"], "isoformat") else str(
        today_row["date"]
    )

    # 1. low sleep score
    if today_row.get("sleep_score") is not None and today_row["sleep_score"] < 50:
        out.append(Detection(
            date=date_str, metric="sleep", kind="low_sleep_score",
            severity="warn", rule="sleep_score<50",
            value=float(today_row["sleep_score"]),
        ))

    # 2. very short sleep
    if today_row.get("sleep_duration_h") is not None and today_row["sleep_duration_h"] < 4.0:
        out.append(Detection(
            date=date_str, metric="sleep", kind="very_short_sleep",
            severity="critical", rule="sleep_duration_h<4",
            value=float(today_row["sleep_duration_h"]),
        ))

    # 3. resting hr spike
    if baseline_row:
        hr_z = _zscore(
            today_row.get("resting_hr"),
            baseline_row.get("resting_hr_avg"),
            baseline_row.get("resting_hr_stddev"),
        )
        if hr_z is not None and hr_z > 2.5:
            out.append(Detection(
                date=date_str, metric="heart_rate", kind="resting_hr_spike",
                severity="warn", rule="resting_hr_z>2.5",
                value=float(today_row["resting_hr"]),
                baseline=baseline_row.get("resting_hr_avg"),
                stddev=baseline_row.get("resting_hr_stddev"),
                z_score=hr_z,
            ))
        hrv_z = _zscore(
            today_row.get("hrv_last_night"),
            baseline_row.get("hrv_avg"),
            baseline_row.get("hrv_stddev"),
        )
        if hrv_z is not None and hrv_z < -2.5:
            out.append(Detection(
                date=date_str, metric="hrv", kind="hrv_drop",
                severity="warn", rule="hrv_z<-2.5",
                value=float(today_row["hrv_last_night"]),
                baseline=baseline_row.get("hrv_avg"),
                stddev=baseline_row.get("hrv_stddev"),
                z_score=hrv_z,
            ))

    # 4. zero steps (only meaningful for past days)
    if today_row.get("steps_total") == 0 and date_str < date.today().isoformat():
        out.append(Detection(
            date=date_str, metric="steps", kind="zero_steps",
            severity="info", rule="steps_total==0",
            value=0.0,
        ))

    return out


def detect_anomalies_for_day(database_url: str, date_str: str) -> list[dict]:
    """Detect anomalies for the day and write them to anomaly_events + notifications.

    Returns the list of new detections as dicts.
    """
    target = date.fromisoformat(date_str)
    baseline_end = (target - timedelta(days=1)).isoformat()
    new_detections: list[Detection] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM derived_daily WHERE date = %s", (date_str,))
            today_row = cur.fetchone()
            if today_row is None:
                return []
            cur.execute("SELECT * FROM rolling_30d WHERE end_date = %s", (baseline_end,))
            baseline = cur.fetchone()
            detections = _detect(today_row, baseline)

            for d in detections:
                cur.execute(
                    """
                    INSERT INTO anomaly_events
                        (date, metric, kind, severity, value, baseline, stddev, z_score, rule)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (date, metric, kind, rule) DO NOTHING
                    """,
                    (
                        d.date, d.metric, d.kind, d.severity, d.value,
                        d.baseline, d.stddev, d.z_score, d.rule,
                    ),
                )
                if cur.rowcount > 0:
                    new_detections.append(d)
                    cur.execute(
                        """
                        INSERT INTO notifications (kind, severity, title, body, payload)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            f"anomaly:{d.kind}",
                            d.severity,
                            f"{d.metric} anomaly on {d.date}",
                            f"{d.kind} ({d.rule}) value={d.value}",
                            json.dumps(asdict(d)),
                        ),
                    )
    return [asdict(d) for d in new_detections]
