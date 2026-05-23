"""Dagster entrypoint: run `dagster dev` with our project as workspace.

We also expose a tiny FastAPI sidecar on a separate port for the
"approval form" — the human submits an OTP for a given pending_id, and we
write it into the `bank_pending` row. The bank_otp_sensor picks it up and
fires the resume job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import psycopg
import uvicorn
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

from pipeline_shared import ensure_schema, load_settings

app = FastAPI(title="pipeline-dagster-approve")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/dagster-approve", response_class=HTMLResponse)
def approve_form(pending_id: int) -> str:
    import httpx
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
    <p>Pending id: <code>{pending_id}</code></p>
    <p>{hint}</p>
    <form method="post" action="/dagster-approve?pending_id={pending_id}">
      <input name="otp" autofocus/>
      <button type="submit">Submit</button>
    </form>
    </body></html>
    """


@app.post("/dagster-approve", response_class=HTMLResponse)
def approve_submit(pending_id: int, otp: str = Form(...)) -> str:
    s = load_settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bank_pending SET otp = %s WHERE id = %s AND status='awaiting_otp'",
                (otp.strip(), pending_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, "pending row not found or already processed")
    return f"<p>OTP recorded for pending #{pending_id}. The sensor will pick it up.</p>"


def _start_approval_sidecar() -> None:
    port = int(os.environ.get("APPROVE_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main() -> None:
    # 1. Make sure schema is present.
    s = load_settings()
    from garmin_fetch.store import GarminStore
    GarminStore(s.database_url).close()
    ensure_schema(s.database_url)

    # 2. Install dagster.yaml into DAGSTER_HOME so the daemon + run launcher
    #    pick up concurrency-pool + run-queue config.
    dagster_home = os.environ.get("DAGSTER_HOME", "/app/dagster/.dagster_home")
    os.makedirs(dagster_home, exist_ok=True)
    src_yaml = "/app/dagster/dagster.yaml"
    dst_yaml = os.path.join(dagster_home, "dagster.yaml")
    if os.path.exists(src_yaml):
        import shutil
        shutil.copyfile(src_yaml, dst_yaml)
        print(f"installed dagster.yaml -> {dst_yaml}", flush=True)

    # 3. Start the approval sidecar in a thread.
    t = threading.Thread(target=_start_approval_sidecar, daemon=True)
    t.start()

    # 4. Launch dagster dev (foreground).
    dag_port = os.environ.get("DAGSTER_PORT", "3000")
    cmd = [
        "dagster", "dev",
        "-h", "0.0.0.0", "-p", dag_port,
        "-m", "pipeline_dagster_proj.definitions",
    ]
    print("starting:", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    sys.exit(rc)


if __name__ == "__main__":
    main()
