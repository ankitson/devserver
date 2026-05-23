"""Mock bank — tiny FastAPI service that mimics a typical web-banking flow.

Endpoints:
  GET  /                       — index
  GET  /login                  — login form
  POST /login                  — accept any non-empty user+pass, set cookie,
                                  generate per-session OTP, redirect to /otp
  GET  /otp                    — OTP entry form (browser sees this)
  POST /otp                    — validate OTP, redirect to /statements
  GET  /statements             — page with CSV download link
  GET  /statements/download.csv — CSV with 7 days of fake transactions

Test-mode helpers (NOT for production banks!):
  GET  /current_otp            — returns latest generated OTP across all sessions
                                  (so the orchestrator's approval UI can display it
                                  back to a human tester)
  GET  /healthz                — health check

Sessions live in an in-memory dict — fine for a single-tenant prototype.
"""

from __future__ import annotations

import io
import csv
import os
import random
import secrets
import string
from datetime import date, timedelta

from fastapi import FastAPI, Form, Request, Response, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse


app = FastAPI(title="mock-bank")

# session_id -> dict(state=login|otp|authed, otp=..., username=..., last_otp_at=...)
SESSIONS: dict[str, dict] = {}
LATEST_OTP: dict[str, str] = {}  # latest OTP generated, per session


def _new_session() -> str:
    return secrets.token_hex(8)


def _new_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def _seed_transactions(seed_date: date | None = None) -> list[dict]:
    """Deterministic-ish fake transactions for the last 7 days."""
    rng = random.Random(20260518)  # stable seed for reproducible tests
    today = seed_date or date.today()
    merchants_small = [
        "TRADER JOES", "STARBUCKS", "AMAZON", "UBER", "CITY METRO",
        "GROCERY MARKET", "CAFE BLEND",
    ]
    merchants_big = [
        "RENT - LANDLORD LLC", "STATE FARM INSURANCE",
        "APPLE STORE - NEW MAC", "BEST BUY",
    ]
    txns = []
    counter = 1
    for offset in range(7, 0, -1):
        d = today - timedelta(days=offset)
        n_small = rng.randint(2, 4)
        for _ in range(n_small):
            txns.append({
                "id": f"TX-{d.isoformat()}-{counter:04d}",
                "posted_date": d.isoformat(),
                "amount": f"-{rng.uniform(3, 80):.2f}",
                "currency": "USD",
                "merchant": rng.choice(merchants_small),
            })
            counter += 1
        if offset == 4:  # one big-ticket purchase mid-week → triggers approval
            txns.append({
                "id": f"TX-{d.isoformat()}-{counter:04d}",
                "posted_date": d.isoformat(),
                "amount": f"-{rng.uniform(900, 2500):.2f}",
                "currency": "USD",
                "merchant": rng.choice(merchants_big),
            })
            counter += 1
        if offset == 6:  # one income deposit
            txns.append({
                "id": f"TX-{d.isoformat()}-{counter:04d}",
                "posted_date": d.isoformat(),
                "amount": f"{rng.uniform(2500, 5000):.2f}",
                "currency": "USD",
                "merchant": "ACME PAYROLL",
            })
            counter += 1
    return txns


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
    <h1>mock-bank</h1>
    <ul>
      <li><a href="/login">Sign in</a></li>
      <li><a href="/healthz">Health</a></li>
    </ul>
    """


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form() -> str:
    return """
    <!doctype html><html><body>
    <h2>mock-bank login</h2>
    <form method="post" action="/login">
      <label>Username: <input name="username" autofocus/></label><br/>
      <label>Password: <input name="password" type="password"/></label><br/>
      <button type="submit">Sign in</button>
    </form>
    </body></html>
    """


@app.post("/login")
def login_submit(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    if not username or not password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    sid = _new_session()
    otp = _new_otp()
    SESSIONS[sid] = {"state": "otp", "username": username, "otp": otp}
    LATEST_OTP[sid] = otp
    # in real life this would be SMS; we log it
    print(f"[mock-bank] session={sid} username={username} otp={otp}")
    redirect = RedirectResponse(url="/otp", status_code=303)
    redirect.set_cookie("mb_session", sid, httponly=True, samesite="lax")
    return redirect


@app.get("/otp", response_class=HTMLResponse)
def otp_form(request: Request) -> str:
    sid = request.cookies.get("mb_session")
    sess = SESSIONS.get(sid or "")
    if not sess or sess["state"] != "otp":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session — log in first")
    masked = "+1***-***-1234"
    return f"""
    <!doctype html><html><body>
    <h2>One-time code</h2>
    <p>We sent a 6-digit code to <span id="destination">{masked}</span>.</p>
    <form method="post" action="/otp">
      <label>Code: <input name="otp" autofocus/></label><br/>
      <button type="submit">Continue</button>
    </form>
    </body></html>
    """


@app.post("/otp")
def otp_submit(request: Request, otp: str = Form(...)) -> Response:
    sid = request.cookies.get("mb_session") or ""
    sess = SESSIONS.get(sid)
    if not sess or sess["state"] != "otp":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    if otp.strip() != sess["otp"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad OTP")
    sess["state"] = "authed"
    return RedirectResponse(url="/statements", status_code=303)


@app.get("/statements", response_class=HTMLResponse)
def statements(request: Request) -> str:
    sid = request.cookies.get("mb_session") or ""
    sess = SESSIONS.get(sid)
    if not sess or sess["state"] != "authed":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return """
    <!doctype html><html><body>
    <h2>Statements</h2>
    <p>Welcome.  Download your latest statement:</p>
    <a id="download-csv" href="/statements/download.csv" download>Download CSV</a>
    </body></html>
    """


@app.get("/statements/download.csv")
def download_csv(request: Request) -> Response:
    sid = request.cookies.get("mb_session") or ""
    sess = SESSIONS.get(sid)
    if not sess or sess["state"] != "authed":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["id", "posted_date", "amount", "currency", "merchant"]
    )
    writer.writeheader()
    for t in _seed_transactions():
        writer.writerow(t)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=statement.csv"},
    )


# --- test-mode helpers ----------------------------------------------------

@app.get("/current_otp", response_class=PlainTextResponse)
def current_otp() -> str:
    """Return the most-recent OTP across all live sessions.

    A real bank does NOT have such an endpoint. This exists ONLY so that the
    orchestrator's human-approval UI can read back what value to enter in test
    mode, simulating a human reading an SMS. Disabled in non-test envs.
    """
    if not LATEST_OTP:
        return ""
    return list(LATEST_OTP.values())[-1]


def main() -> None:
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
