"""Derived (L3) layer.

All computations are pure SQL on parsed tables. Each derived table is
idempotently upserted per-day. Functions are short and stateless so the
orchestrator decides scheduling (downstream of L2 materializations).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import psycopg

log = logging.getLogger(__name__)

# --- daily summary --------------------------------------------------------

_DAILY_SUMMARY_SQL = """
INSERT INTO derived_daily (
    date, sleep_score, sleep_duration_h, hrv_last_night, hrv_weekly,
    resting_hr, stress_overall, body_battery_high, body_battery_low,
    steps_total, readiness_score, readiness_level, computed_at
)
SELECT
    %(date)s::date AS date,
    s.sleep_score,
    s.duration_secs::real / 3600.0 AS sleep_duration_h,
    h.last_night_avg AS hrv_last_night,
    h.weekly_avg AS hrv_weekly,
    hr.resting AS resting_hr,
    st.overall_level AS stress_overall,
    bb.high AS body_battery_high,
    bb.low AS body_battery_low,
    sp.total AS steps_total,
    tr.score AS readiness_score,
    tr.level AS readiness_level,
    NOW()
FROM (SELECT %(date)s::date AS d) day
LEFT JOIN sleep              s  ON s.date  = day.d
LEFT JOIN hrv                h  ON h.date  = day.d
LEFT JOIN heart_rate         hr ON hr.date = day.d
LEFT JOIN stress             st ON st.date = day.d
LEFT JOIN body_battery       bb ON bb.date = day.d
LEFT JOIN steps              sp ON sp.date = day.d
LEFT JOIN training_readiness tr ON tr.date = day.d
ON CONFLICT (date) DO UPDATE SET
    sleep_score = EXCLUDED.sleep_score,
    sleep_duration_h = EXCLUDED.sleep_duration_h,
    hrv_last_night = EXCLUDED.hrv_last_night,
    hrv_weekly = EXCLUDED.hrv_weekly,
    resting_hr = EXCLUDED.resting_hr,
    stress_overall = EXCLUDED.stress_overall,
    body_battery_high = EXCLUDED.body_battery_high,
    body_battery_low = EXCLUDED.body_battery_low,
    steps_total = EXCLUDED.steps_total,
    readiness_score = EXCLUDED.readiness_score,
    readiness_level = EXCLUDED.readiness_level,
    computed_at = NOW();
"""

# --- rolling windows ------------------------------------------------------

_ROLLING_SQL = """
WITH win AS (
    SELECT
        date,
        sleep_score,
        sleep_duration_h,
        hrv_last_night,
        resting_hr,
        stress_overall,
        steps_total
    FROM derived_daily
    WHERE date BETWEEN %(start)s AND %(end)s
)
INSERT INTO {table} (
    end_date, sleep_score_avg, sleep_duration_h_avg, hrv_avg, resting_hr_avg,
    stress_avg, steps_avg, computed_at{extra_cols}
)
SELECT
    %(end)s::date,
    AVG(sleep_score)::real,
    AVG(sleep_duration_h)::real,
    AVG(hrv_last_night)::real,
    AVG(resting_hr)::real,
    AVG(stress_overall)::real,
    AVG(steps_total)::real,
    NOW(){extra_vals}
FROM win
ON CONFLICT (end_date) DO UPDATE SET
    sleep_score_avg = EXCLUDED.sleep_score_avg,
    sleep_duration_h_avg = EXCLUDED.sleep_duration_h_avg,
    hrv_avg = EXCLUDED.hrv_avg,
    resting_hr_avg = EXCLUDED.resting_hr_avg,
    stress_avg = EXCLUDED.stress_avg,
    steps_avg = EXCLUDED.steps_avg,
    computed_at = NOW(){extra_updates};
"""

_ROLLING_30D_EXTRAS = {
    "extra_cols": (
        ", sleep_score_stddev, hrv_stddev, resting_hr_stddev"
    ),
    "extra_vals": (
        ", STDDEV_POP(sleep_score)::real"
        ", STDDEV_POP(hrv_last_night)::real"
        ", STDDEV_POP(resting_hr)::real"
    ),
    "extra_updates": (
        ", sleep_score_stddev = EXCLUDED.sleep_score_stddev"
        ", hrv_stddev = EXCLUDED.hrv_stddev"
        ", resting_hr_stddev = EXCLUDED.resting_hr_stddev"
    ),
}


def refresh_derived_for_day(database_url: str, date_str: str) -> dict[str, int]:
    """Compute derived_daily + rolling_7d + rolling_30d for the given date.

    Returns row-affected counts per table.
    """
    target = date.fromisoformat(date_str)
    rolling_7d_start = (target - timedelta(days=6)).isoformat()
    rolling_30d_start = (target - timedelta(days=29)).isoformat()
    out: dict[str, int] = {}
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_DAILY_SUMMARY_SQL, {"date": date_str})
            out["derived_daily"] = cur.rowcount
            cur.execute(
                _ROLLING_SQL.format(
                    table="rolling_7d", extra_cols="", extra_vals="", extra_updates=""
                ),
                {"start": rolling_7d_start, "end": date_str},
            )
            out["rolling_7d"] = cur.rowcount
            cur.execute(
                _ROLLING_SQL.format(table="rolling_30d", **_ROLLING_30D_EXTRAS),
                {"start": rolling_30d_start, "end": date_str},
            )
            out["rolling_30d"] = cur.rowcount
    return out
