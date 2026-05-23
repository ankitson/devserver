"""Combined entry: Restate SDK app (port 9080) + FastAPI sidecar (port 8001).

The sidecar:
  - GET /restate-approve?otp_id=...  — HTML form
  - POST /restate-approve?otp_id=... — submits OTP via Restate's awakeable resolution endpoint
  - GET /healthz
  - POST /trigger/bank_import — convenience: starts the BankImport workflow with a generated id
  - POST /trigger/fetch_day   — convenience: calls GarminIngest.fetch_day
"""

import asyncio
import logging
import os
import threading
import uuid

import httpx
import psycopg
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

from pipeline_shared import ensure_schema, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Bootstrap schema before any handler is invoked.
_settings_at_boot = load_settings()
from garmin_fetch.store import GarminStore  # noqa: E402

GarminStore(_settings_at_boot.database_url).close()
ensure_schema(_settings_at_boot.database_url)

# Restate ingress URL (used by the sidecar to start workflows and resolve awakeables)
RESTATE_INGRESS = os.environ.get("RESTATE_INGRESS_URL", "http://restate-server:8080")

app = FastAPI(title="pipeline-restate-sidecar")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/trigger/bank_import")
def trigger_bank_import(
    bank_name: str = Form("mock-bank"),
    username: str = Form("ankit"),
    password: str = Form("test"),
) -> dict:
    wf_id = str(uuid.uuid4())
    body = {"bank_name": bank_name, "username": username, "password": password}
    url = f"{RESTATE_INGRESS}/BankImport/{wf_id}/run/send"
    r = httpx.post(url, json=body, timeout=10.0)
    r.raise_for_status()
    return {"workflow_id": wf_id, "ingress_response": r.json()}


@app.post("/trigger/fetch_day")
def trigger_fetch_day(date: str = Form(...)) -> dict:
    url = f"{RESTATE_INGRESS}/GarminIngest/fetch_day/send"
    r = httpx.post(url, json=date, timeout=10.0)
    r.raise_for_status()
    return {"ingress_response": r.json()}


@app.post("/trigger/fetch_window")
def trigger_fetch_window(start: str = Form(...), end: str = Form(...)) -> dict:
    url = f"{RESTATE_INGRESS}/GarminIngest/fetch_window/send"
    r = httpx.post(url, json={"start": start, "end": end}, timeout=10.0)
    r.raise_for_status()
    return {"ingress_response": r.json()}


@app.get("/restate-approve", response_class=HTMLResponse)
def approve_form(otp_id: str) -> str:
    hint = ""
    try:
        s = load_settings()
        r = httpx.get(f"{s.mock_bank_url.rstrip('/')}/current_otp", timeout=2.0)
        if r.status_code == 200 and r.text.strip():
            hint = f"(test-mode hint: current OTP is <code>{r.text.strip()}</code>)"
    except Exception:
        pass
    return f"""
    <html><body>
    <h2>Approve bank import</h2>
    <p>Awakeable: <code>{otp_id}</code></p>
    <p>{hint}</p>
    <form method="post" action="/restate-approve?otp_id={otp_id}">
      <input name="otp" autofocus/>
      <button type="submit">Submit</button>
    </form>
    </body></html>
    """


@app.post("/restate-approve", response_class=HTMLResponse)
def approve_submit(otp_id: str, otp: str = Form(...)) -> str:
    # Resolve the awakeable via Restate's ingress.
    url = f"{RESTATE_INGRESS}/restate/awakeables/{otp_id}/resolve"
    r = httpx.post(url, json=otp.strip(), timeout=10.0)
    r.raise_for_status()
    return f"<p>OTP delivered to awakeable <code>{otp_id}</code>.</p>"


@app.get("/transactions", response_class=HTMLResponse)
def transactions() -> str:
    with psycopg.connect(_settings_at_boot.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_id, posted_date, amount_cents, merchant, category, status
              FROM transactions ORDER BY posted_date DESC, id DESC LIMIT 50
            """
        )
        rows = cur.fetchall()
    lines = ["<h2>transactions</h2><table border=1 cellpadding=4>"]
    lines.append("<tr><th>ext</th><th>date</th><th>amount</th>"
                 "<th>merchant</th><th>cat</th><th>status</th></tr>")
    for r in rows:
        amt = f"{r[2]/100:.2f}"
        lines.append(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{amt}</td>"
                     f"<td>{r[3]}</td><td>{r[4]}</td><td>{r[5]}</td></tr>")
    lines.append("</table>")
    return "".join(lines)


def _start_sidecar_in_thread() -> None:
    """FastAPI sidecar runs in a thread so the main thread holds the Restate
    SDK loop (Hypercorn needs signal handlers, which only work in main thread)."""
    import uvicorn
    port = int(os.environ.get("APPROVE_PORT", "8001"))
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info",
                          loop="asyncio")
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())


def main() -> None:
    import hypercorn.asyncio
    import hypercorn.config
    from pipeline_restate_proj.services import restate_app
    t = threading.Thread(target=_start_sidecar_in_thread, daemon=True)
    t.start()
    cfg = hypercorn.config.Config()
    cfg.bind = ["0.0.0.0:9080"]
    asyncio.run(hypercorn.asyncio.serve(restate_app, cfg))


if __name__ == "__main__":
    main()
