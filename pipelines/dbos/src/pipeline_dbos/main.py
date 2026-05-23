"""DBOS entrypoint: FastAPI app + DBOS init + small admin UI.

Endpoints:
  GET  /                          tiny landing page with links
  GET  /healthz
  POST /trigger/fetch_day         body {"date": "YYYY-MM-DD"}
  POST /trigger/fetch_window      body {"start": "...", "end": "..."}
  POST /trigger/reparse           same as fetch_window (alias)
  POST /trigger/derive            body {"date": "..."}
  POST /trigger/detect            body {"date": "..."}
  POST /trigger/bank_import       body {"bank_name", "username", "password"}
  GET  /approve?wf=<workflow_id>  HTML form for human to supply OTP
  POST /approve?wf=<workflow_id>  submits OTP into DBOS.send
  GET  /runs                      last 50 workflow rows
  GET  /transactions              last 50 transactions
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import psycopg
from dbos import DBOS, DBOSConfig
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pipeline_shared import (
    ensure_schema,
    load_settings,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Ensure shared/extra schema exists before DBOS bootstraps.
_settings_at_boot = load_settings()
from garmin_fetch.store import GarminStore  # noqa: E402
GarminStore(_settings_at_boot.database_url).close()
ensure_schema(_settings_at_boot.database_url)

app = FastAPI(title="pipeline-dbos")

# DBOS config — uses the same DB for both pipeline data and workflow state.
_dbos_config: DBOSConfig = {
    "name": "pipeline-dbos",
    "database_url": _settings_at_boot.database_url,
}
DBOS(fastapi=app, config=_dbos_config)

# Import workflows so the @DBOS.workflow / @DBOS.scheduled registrations fire.
from pipeline_dbos import workflows  # noqa: E402, F401


_CSS = """
<style>
body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
       background: #f6f7f9; color: #1a1a1a; }
header { background: #1a1a1a; color: white; padding: 12px 20px;
         display: flex; gap: 24px; align-items: center; }
header a { color: #cce; text-decoration: none; }
header a:hover { color: white; }
header h1 { margin: 0; font-size: 18px; font-weight: 600; }
main { padding: 16px 20px; max-width: 1200px; }
section { background: white; border: 1px solid #e0e2e6; border-radius: 6px;
          padding: 16px; margin-bottom: 16px; }
section h2 { margin: 0 0 12px; font-size: 14px; text-transform: uppercase;
             color: #555; letter-spacing: 0.5px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; }
.card { background: #fafafa; border: 1px solid #e8e8ea; border-radius: 4px;
        padding: 10px 12px; }
.card .label { font-size: 11px; text-transform: uppercase; color: #666; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.card .sub { font-size: 11px; color: #888; margin-top: 2px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
table th, table td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #eee; }
table th { background: #f8f8fa; font-weight: 600; }
.status-ok, .status-success, .status-SUCCESS, .status-COMPLETED, .status-completed
{ color: #0a7d2c; font-weight: 600; }
.status-error, .status-ERROR, .status-failed { color: #b91c1c; font-weight: 600; }
.status-PENDING, .status-running, .status-pending_approval, .status-awaiting_otp
{ color: #c2410c; font-weight: 600; }
.muted { color: #999; }
form.inline { display: inline-flex; gap: 6px; align-items: center; }
form input { padding: 4px 6px; border: 1px solid #ccc; border-radius: 3px; font-size: 12px; }
form button { padding: 4px 10px; border: 1px solid #444; border-radius: 3px;
              background: #1a1a1a; color: white; cursor: pointer; font-size: 12px; }
form button:hover { background: #333; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 3px;
       background: #eef; color: #336; font-size: 11px; font-weight: 600; }
.warn { background: #fef3c7; color: #92400e; }
.crit { background: #fecaca; color: #991b1b; }
</style>
"""


def _nav(active: str = "") -> str:
    items = [
        ("/", "Dashboard"),
        ("/runs", "Workflows"),
        ("/transactions", "Transactions"),
        ("/anomalies", "Anomalies"),
        ("/notifications", "Notifications"),
    ]
    links = " · ".join(
        f'<a href="{href}" style="{"color:white" if href==active else ""}">{label}</a>'
        for href, label in items
    )
    return f"""
    <header>
      <h1>pipeline-dbos</h1>
      {links}
      <span style="margin-left:auto;color:#aaa;font-size:12px">
        DBOS · Postgres-backed durable execution
      </span>
    </header>
    """


def _page(title: str, body: str, active: str = "") -> str:
    return f"""<!doctype html><html><head><title>{title}</title>{_CSS}</head>
    <body>{_nav(active)}<main>{body}</main></body></html>"""


def _safe_count(cur, table: str) -> int | None:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return None


def _safe_max(cur, table: str, col: str) -> str | None:
    try:
        cur.execute(f"SELECT MAX({col})::text FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    s = _settings_at_boot
    cards: list[tuple[str, str, str]] = []
    fails: list[tuple[str, str, str]] = []
    recent_runs: list[tuple] = []

    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        conn.autocommit = True
        # Garmin data coverage
        sleep_n = _safe_count(cur, "sleep") or 0
        sleep_min = _safe_max(cur, "sleep", "MIN(date)") or ""
        sleep_max = _safe_max(cur, "sleep", "date") or ""
        cards.append(("Garmin days (sleep)", str(sleep_n),
                      f"latest: {sleep_max}"))

        for tbl, label in [("heart_rate", "Heart-rate days"),
                            ("hrv", "HRV days"),
                            ("derived_daily", "Derived days"),
                            ("anomaly_events", "Anomalies"),
                            ("raw_responses", "Raw cache rows")]:
            n = _safe_count(cur, tbl) or 0
            sub = _safe_max(cur, tbl, "date" if tbl != "anomaly_events"
                            else "MAX(date)") or ""
            cards.append((label, str(n), f"latest: {sub}" if sub else ""))

        # Transactions
        cur.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(amount_cents)/100.0,0) "
            "FROM transactions GROUP BY status"
        )
        for st, n, total in cur.fetchall():
            cards.append((f"Txn — {st}", str(n), f"${float(total):,.2f}"))

        # Notifications
        cur.execute("SELECT severity, COUNT(*) FROM notifications "
                    "WHERE delivered_at IS NULL GROUP BY severity")
        for sev, n in cur.fetchall():
            cards.append((f"Notif undelivered — {sev}", str(n), ""))

        # Recent failures (last 24h)
        try:
            cur.execute(
                """
                SELECT asset, partition_key, status, error, started_at
                  FROM pipeline_runs
                 WHERE status LIKE 'error%' OR error IS NOT NULL
                 ORDER BY started_at DESC LIMIT 10
                """
            )
            fails = [(r[0], r[1], r[2] or "", r[3] or "", str(r[4])[:19])
                     for r in cur.fetchall()]
        except Exception:
            conn.rollback()

    # DBOS workflow status from the sys DB
    workflow_summary: dict[str, int] = {}
    try:
        sys_url = s.database_url.replace(
            "/pipeline_dbos", "/pipeline_dbos_dbos_sys"
        )
        with psycopg.connect(sys_url) as sc, sc.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM dbos.workflow_status GROUP BY status"
            )
            workflow_summary = dict(cur.fetchall())
    except Exception as e:
        log.warning("sys DB unreachable: %s", e)

    for st, n in workflow_summary.items():
        cards.append((f"DBOS — {st}", str(n), ""))

    cards_html = "".join(
        f'<div class="card"><div class="label">{l}</div>'
        f'<div class="value">{v}</div><div class="sub">{s}</div></div>'
        for l, v, s in cards
    )
    fails_html = "".join(
        f'<tr><td>{a}</td><td>{p or ""}</td>'
        f'<td><span class="status-error">{s}</span></td>'
        f'<td>{(e or "")[:160]}</td><td class="muted">{t}</td></tr>'
        for a, p, s, e, t in fails
    ) or '<tr><td colspan="5" class="muted">no recent failures</td></tr>'

    body = f"""
    <section><h2>Data status</h2>
      <div class="cards">{cards_html}</div>
    </section>

    <section><h2>Recent failures</h2>
      <table>
        <tr><th>asset</th><th>partition</th><th>status</th>
            <th>error</th><th>when</th></tr>
        {fails_html}
      </table>
    </section>

    <section class="grid-2">
      <div>
        <h2>Trigger Garmin reparse</h2>
        <form method="post" action="/trigger/fetch_day" class="inline">
          <input name="date" placeholder="YYYY-MM-DD" required/>
          <button type="submit">fetch_day</button>
        </form>
        <br/><br/>
        <form method="post" action="/trigger/fetch_window" class="inline">
          <input name="start" placeholder="start" required/>
          <input name="end" placeholder="end" required/>
          <button type="submit">fetch_window</button>
        </form>
      </div>
      <div>
        <h2>Trigger bank import</h2>
        <form method="post" action="/trigger/bank_import" class="inline">
          <input name="bank_name" value="mock-bank"/>
          <input name="username" value="ankit"/>
          <input name="password" value="test"/>
          <button type="submit">import</button>
        </form>
      </div>
    </section>
    """
    return _page("pipeline-dbos · dashboard", body, "/")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _start(workflow: str, **kwargs: Any) -> JSONResponse:
    from pipeline_dbos.workflows import (
        fetch_day_workflow,
        fetch_window_workflow,
        import_bank_statement,
    )
    from pipeline_shared import (
        refresh_derived_for_day,
        detect_anomalies_for_day,
    )

    if workflow == "fetch_day":
        h = DBOS.start_workflow(fetch_day_workflow, kwargs["date"])
        return JSONResponse({"workflow_id": h.workflow_id})
    if workflow == "fetch_window":
        h = DBOS.start_workflow(fetch_window_workflow, kwargs["start"], kwargs["end"])
        return JSONResponse({"workflow_id": h.workflow_id})
    if workflow == "bank_import":
        h = DBOS.start_workflow(
            import_bank_statement, kwargs["bank_name"], kwargs["username"],
            kwargs["password"],
        )
        return JSONResponse({"workflow_id": h.workflow_id})
    raise HTTPException(404)


@app.post("/trigger/fetch_day")
def trigger_fetch_day(date: str = Form(...)) -> JSONResponse:
    return _start("fetch_day", date=date)


@app.post("/trigger/fetch_window")
def trigger_fetch_window(start: str = Form(...), end: str = Form(...)) -> JSONResponse:
    return _start("fetch_window", start=start, end=end)


@app.post("/trigger/reparse")
def trigger_reparse(start: str = Form(...), end: str = Form(...)) -> JSONResponse:
    return _start("fetch_window", start=start, end=end)


@app.post("/trigger/derive")
def trigger_derive(date: str = Form(...)) -> dict:
    from pipeline_shared import refresh_derived_for_day
    return refresh_derived_for_day(_settings_at_boot.database_url, date)


@app.post("/trigger/detect")
def trigger_detect(date: str = Form(...)) -> list[dict]:
    from pipeline_shared import detect_anomalies_for_day
    return detect_anomalies_for_day(_settings_at_boot.database_url, date)


@app.post("/trigger/bank_import")
def trigger_bank_import(
    bank_name: str = Form("mock-bank"),
    username: str = Form("ankit"),
    password: str = Form("test"),
) -> JSONResponse:
    return _start(
        "bank_import", bank_name=bank_name, username=username, password=password
    )


# --- approval ------------------------------------------------------------

_APPROVE_PAGE = """
<!doctype html><html><body>
<h2>Approve bank import</h2>
<p>Workflow: <code>{wf_id}</code></p>
<p>{hint}</p>
<form method="post" action="/approve?wf={wf_id}">
  <label>Enter the OTP from your phone:</label>
  <input name="otp" autofocus/>
  <button type="submit">Submit</button>
</form>
</body></html>
"""


@app.get("/approve", response_class=HTMLResponse)
def approve_form(wf: str) -> str:
    # test mode helper: read the mock bank's "current OTP" so the page can hint
    s = _settings_at_boot
    hint = ""
    try:
        r = httpx.get(f"{s.mock_bank_url.rstrip('/')}/current_otp", timeout=2.0)
        if r.status_code == 200 and r.text.strip():
            hint = f"(test-mode hint: current OTP is <code>{r.text.strip()}</code>)"
    except Exception:
        pass
    return _APPROVE_PAGE.format(wf_id=wf, hint=hint)


@app.post("/approve", response_class=HTMLResponse)
def approve_submit(wf: str, otp: str = Form(...)) -> str:
    DBOS.send(wf, otp.strip(), topic="otp")
    return (
        f"<p>Sent OTP to workflow <code>{wf}</code>. "
        f"<a href='/runs'>View runs</a></p>"
    )


# --- read views ----------------------------------------------------------

@app.get("/runs", response_class=HTMLResponse)
def runs(name: str = "", status: str = "", limit: int = 100) -> str:
    s = _settings_at_boot
    # DBOS workflow status lives in the system DB.
    sys_url = s.database_url.replace(
        "/pipeline_dbos", "/pipeline_dbos_dbos_sys"
    )
    where = []
    params: list = []
    if name:
        where.append("name LIKE %s")
        params.append(f"%{name}%")
    if status:
        where.append("status = %s")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with psycopg.connect(sys_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT workflow_uuid, status, name,
                   to_timestamp(created_at / 1000) AT TIME ZONE 'UTC',
                   to_timestamp(updated_at / 1000) AT TIME ZONE 'UTC',
                   LEFT(COALESCE(error, ''), 200)
              FROM dbos.workflow_status
            {where_sql}
             ORDER BY created_at DESC LIMIT %s
            """,
            (*params, limit),
        )
        rows = cur.fetchall()
    rows_html = "".join(
        f'<tr><td><code style="font-size:11px">{r[0]}</code></td>'
        f'<td><span class="status-{r[1]}">{r[1]}</span></td>'
        f'<td>{r[2]}</td>'
        f'<td class="muted">{str(r[3])[:19]}</td>'
        f'<td class="muted">{str(r[4])[:19]}</td>'
        f'<td>{r[5][:80] if r[5] else ""}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="6" class="muted">no workflows yet</td></tr>'
    filter_form = f"""
    <form method="get" class="inline">
      <input name="name" value="{name}" placeholder="name filter"/>
      <input name="status" value="{status}" placeholder="status (PENDING/SUCCESS/ERROR)"/>
      <input name="limit" value="{limit}" type="number" min="10" max="500"/>
      <button type="submit">filter</button>
    </form>
    """
    body = f"""
    <section>
      <h2>Filter</h2>
      {filter_form}
    </section>
    <section>
      <h2>Workflows · {len(rows)} shown</h2>
      <table>
        <tr><th>workflow id</th><th>status</th><th>name</th>
            <th>created</th><th>updated</th><th>error</th></tr>
        {rows_html}
      </table>
    </section>
    """
    return _page("pipeline-dbos · workflows", body, "/runs")


@app.get("/anomalies", response_class=HTMLResponse)
def anomalies() -> str:
    s = _settings_at_boot
    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, metric, kind, severity, value, baseline,
                   z_score, rule, detected_at
              FROM anomaly_events ORDER BY detected_at DESC LIMIT 100
            """
        )
        rows = cur.fetchall()
    rows_html = "".join(
        f'<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>'
        f'<td><span class="tag warn">{r[3]}</span></td>'
        f'<td>{r[4]}</td><td class="muted">{r[5] or ""}</td>'
        f'<td>{f"{r[6]:.2f}" if r[6] is not None else ""}</td>'
        f'<td><code style="font-size:11px">{r[7]}</code></td>'
        f'<td class="muted">{str(r[8])[:19]}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="9" class="muted">no anomalies detected</td></tr>'
    body = f"""
    <section>
      <h2>Anomaly events · {len(rows)} shown</h2>
      <table>
        <tr><th>date</th><th>metric</th><th>kind</th><th>severity</th>
            <th>value</th><th>baseline</th><th>z</th><th>rule</th>
            <th>detected</th></tr>
        {rows_html}
      </table>
    </section>
    """
    return _page("pipeline-dbos · anomalies", body, "/anomalies")


@app.get("/notifications", response_class=HTMLResponse)
def notifications() -> str:
    s = _settings_at_boot
    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, created_at, kind, severity, title, body,
                   delivered_at, delivered_via
              FROM notifications ORDER BY created_at DESC LIMIT 100
            """
        )
        rows = cur.fetchall()
    rows_html = ""
    for r in rows:
        sev_class = ("crit" if r[3] == "critical"
                      else "warn" if r[3] == "warn" else "")
        delivered = (f'<span class="muted">delivered via {r[7]} at {str(r[6])[:19]}</span>'
                      if r[6] else '<span class="status-pending_approval">pending</span>')
        rows_html += (
            f'<tr><td>{r[0]}</td><td class="muted">{str(r[1])[:19]}</td>'
            f'<td>{r[2]}</td>'
            f'<td><span class="tag {sev_class}">{r[3]}</span></td>'
            f'<td>{r[4] or ""}</td>'
            f'<td>{r[5] or ""}</td><td>{delivered}</td></tr>'
        )
    body = f"""
    <section>
      <h2>Notifications · {len(rows)} shown</h2>
      <table>
        <tr><th>id</th><th>created</th><th>kind</th><th>sev</th>
            <th>title</th><th>body</th><th>delivered?</th></tr>
        {rows_html or '<tr><td colspan="7" class="muted">no notifications</td></tr>'}
      </table>
    </section>
    """
    return _page("pipeline-dbos · notifications", body, "/notifications")


@app.get("/transactions", response_class=HTMLResponse)
def transactions() -> str:
    s = _settings_at_boot
    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, external_id, posted_date, amount_cents, merchant,
                   category, status
              FROM transactions
             ORDER BY posted_date DESC, id DESC LIMIT 200
            """
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT status, COUNT(*), SUM(amount_cents)/100.0 FROM transactions "
            "GROUP BY status"
        )
        summary = cur.fetchall()
    summary_html = "".join(
        f'<div class="card"><div class="label">{r[0]}</div>'
        f'<div class="value">{r[1]}</div>'
        f'<div class="sub">${float(r[2]):,.2f}</div></div>'
        for r in summary
    )
    rows_html = ""
    for r in rows:
        amt = r[3] / 100.0
        amt_color = "#0a7d2c" if amt > 0 else "#1a1a1a"
        rows_html += (
            f'<tr><td>{r[0]}</td><td><code style="font-size:11px">{r[1]}</code></td>'
            f'<td>{r[2]}</td>'
            f'<td style="color:{amt_color};font-variant-numeric:tabular-nums">'
            f'{amt:+.2f}</td>'
            f'<td>{r[4]}</td><td>{r[5]}</td>'
            f'<td><span class="status-{r[6]}">{r[6]}</span></td></tr>'
        )
    body = f"""
    <section>
      <h2>Summary</h2>
      <div class="cards">{summary_html}</div>
    </section>
    <section>
      <h2>Transactions · {len(rows)} shown</h2>
      <table>
        <tr><th>id</th><th>ext</th><th>date</th><th>amount</th>
            <th>merchant</th><th>category</th><th>status</th></tr>
        {rows_html or '<tr><td colspan="7" class="muted">no transactions</td></tr>'}
      </table>
    </section>
    """
    return _page("pipeline-dbos · transactions", body, "/transactions")


def main() -> None:
    import uvicorn
    DBOS.launch()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
