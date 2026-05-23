"""Banking pipeline primitives.

The orchestrator-agnostic pieces:

  - `BankImportRequest`        — input contract (bank_name, credentials_ref, etc.)
  - `OtpRequest` / `OtpAwaiter` — Protocol abstraction so each orchestrator can
                                  plug in its own durable-pause primitive
                                  (DBOS.recv, Restate awakeable, Dagster sensor).
  - `run_bank_import`          — the actual orchestration body. Drives Playwright,
                                  posts an `OtpRequest` to the awaiter, waits,
                                  resumes the browser, downloads the CSV.
  - `process_statement_csv`    — parses a downloaded CSV → transaction rows,
                                  upserts into the `transactions` table.

The split is deliberate: `run_bank_import` is a normal Python function that
returns the path to the downloaded statement. Durable execution is *outside*
this function — the orchestrator decides where to checkpoint.

`OtpAwaiter` is a tiny protocol. Each tool's adapter wraps:
  - DBOS: `DBOS.set_event("approval_token", id) + DBOS.recv("otp", timeout)`
  - Restate: `id, promise = ctx.awakeable(); await promise`
  - Dagster: insert row in `bank_imports`, sensor polls, run resumes via re-trigger
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import psycopg

from pipeline_shared.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BankImportRequest:
    bank_name: str
    username: str
    password: str
    download_dir: str = "/tmp/bank-downloads"


class AsyncOtpAwaiter(Protocol):
    """Async variant of OtpAwaiter — used by orchestrators that run on an
    event loop (DBOS, Restate). Same contract; just `await`-able."""

    def workflow_id(self) -> str: ...

    async def request_otp(self, req: "OtpRequest") -> None: ...

    async def wait_for_otp(self, timeout_seconds: int) -> str: ...


@dataclass(frozen=True)
class OtpRequest:
    """Sent to the user when the bank challenges with OTP."""
    workflow_id: str
    bank_name: str
    masked_destination: str  # "+1***-***-1234"
    prompt_url: str          # where the human enters the OTP


class OtpAwaiter(Protocol):
    """Each orchestrator implements this around its durable primitive."""

    def workflow_id(self) -> str: ...

    def request_otp(self, req: OtpRequest) -> None:
        """Persist + notify. The actual push happens via the Notifier."""

    def wait_for_otp(self, timeout_seconds: int) -> str:
        """Block (durably!) until OTP is supplied. Return the OTP string.
        Raises TimeoutError if no OTP arrives in time.
        """


# --- the orchestration body ----------------------------------------------

def run_bank_import(
    *,
    settings: Settings,
    request: BankImportRequest,
    awaiter: OtpAwaiter,
    otp_timeout_seconds: int = 60 * 60,  # 1 hour default
) -> str:
    """Drive a Playwright session against the mock bank, pause for human OTP,
    finish login, download CSV. Returns the local path to the downloaded file.

    Playwright is imported lazily so this module imports cheap even if the
    orchestrator doesn't have the browser bundled.
    """
    from playwright.sync_api import sync_playwright

    download_dir = Path(request.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    statement_path: Path | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless_browser)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            log.info("navigating to mock bank: %s", settings.mock_bank_url)
            page.goto(f"{settings.mock_bank_url}/login")
            page.fill('input[name="username"]', request.username)
            page.fill('input[name="password"]', request.password)
            page.click('button[type="submit"]')
            page.wait_for_url(f"{settings.mock_bank_url}/otp", timeout=15000)

            masked = page.locator("#destination").inner_text(timeout=5000)
            wf_id = awaiter.workflow_id()
            prompt_url = (
                f"{settings.mock_bank_url}/approve?wf={wf_id}"  # human-friendly link
            )
            awaiter.request_otp(OtpRequest(
                workflow_id=wf_id,
                bank_name=request.bank_name,
                masked_destination=masked,
                prompt_url=prompt_url,
            ))
            log.info("waiting for OTP for workflow %s (timeout=%ds)", wf_id, otp_timeout_seconds)
            otp = awaiter.wait_for_otp(timeout_seconds=otp_timeout_seconds)

            page.fill('input[name="otp"]', otp)
            page.click('button[type="submit"]')
            page.wait_for_url(
                f"{settings.mock_bank_url}/statements", timeout=15000
            )
            with page.expect_download() as dl_info:
                page.click('a#download-csv')
            download = dl_info.value
            target = download_dir / f"{request.bank_name}-{wf_id}.csv"
            download.save_as(str(target))
            statement_path = target
            log.info("downloaded statement to %s", target)
        finally:
            browser.close()

    if statement_path is None:
        raise RuntimeError("Statement download failed")
    return str(statement_path)


async def bank_login_and_pause(
    *,
    settings: Settings,
    request: BankImportRequest,
) -> dict:
    """Drive Playwright: login → reach OTP page → save cookies → close browser.

    Returns:
        {"storage_state": <dict>, "masked_destination": "<str>"}

    Caller is expected to durably wait for the human OTP, then call
    bank_resume_and_download to finish.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless_browser)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            await page.goto(f"{settings.mock_bank_url}/login")
            await page.fill('input[name="username"]', request.username)
            await page.fill('input[name="password"]', request.password)
            await page.click('button[type="submit"]')
            await page.wait_for_url(f"{settings.mock_bank_url}/otp", timeout=15000)
            masked = await page.locator("#destination").inner_text(timeout=5000)
            storage_state = await context.storage_state()
            return {"storage_state": storage_state, "masked_destination": masked}
        finally:
            await browser.close()


async def bank_resume_and_download(
    *,
    settings: Settings,
    request: BankImportRequest,
    storage_state: dict,
    otp: str,
    download_path_prefix: str,
) -> str:
    """Re-launch browser with prior cookies → fill OTP → download CSV → return path."""
    from playwright.async_api import async_playwright

    download_dir = Path(request.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    target = download_dir / f"{download_path_prefix}.csv"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless_browser)
        context = await browser.new_context(
            accept_downloads=True, storage_state=storage_state,
        )
        page = await context.new_page()
        try:
            await page.goto(f"{settings.mock_bank_url}/otp")
            await page.fill('input[name="otp"]', otp)
            await page.click('button[type="submit"]')
            await page.wait_for_url(
                f"{settings.mock_bank_url}/statements", timeout=15000
            )
            async with page.expect_download() as dl_info:
                await page.click('a#download-csv')
            download = await dl_info.value
            await download.save_as(str(target))
        finally:
            await browser.close()
    return str(target)


async def run_bank_import_async(
    *,
    settings: Settings,
    request: BankImportRequest,
    awaiter: AsyncOtpAwaiter,
    otp_timeout_seconds: int = 60 * 60,
) -> str:
    """Async variant of run_bank_import. Uses Playwright's async API throughout."""
    from playwright.async_api import async_playwright

    download_dir = Path(request.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    statement_path: Path | None = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless_browser)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            log.info("navigating to mock bank (async): %s", settings.mock_bank_url)
            await page.goto(f"{settings.mock_bank_url}/login")
            await page.fill('input[name="username"]', request.username)
            await page.fill('input[name="password"]', request.password)
            await page.click('button[type="submit"]')
            await page.wait_for_url(f"{settings.mock_bank_url}/otp", timeout=15000)
            masked = await page.locator("#destination").inner_text(timeout=5000)
            wf_id = awaiter.workflow_id()
            prompt_url = f"{settings.mock_bank_url}/approve?wf={wf_id}"
            await awaiter.request_otp(OtpRequest(
                workflow_id=wf_id, bank_name=request.bank_name,
                masked_destination=masked, prompt_url=prompt_url,
            ))
            log.info("waiting (async) for OTP for workflow %s", wf_id)
            otp = await awaiter.wait_for_otp(timeout_seconds=otp_timeout_seconds)
            await page.fill('input[name="otp"]', otp)
            await page.click('button[type="submit"]')
            await page.wait_for_url(
                f"{settings.mock_bank_url}/statements", timeout=15000
            )
            async with page.expect_download() as dl_info:
                await page.click('a#download-csv')
            download = await dl_info.value
            target = download_dir / f"{request.bank_name}-{wf_id}.csv"
            await download.save_as(str(target))
            statement_path = target
            log.info("downloaded statement to %s", target)
        finally:
            await browser.close()

    if statement_path is None:
        raise RuntimeError("Statement download failed")
    return str(statement_path)


# --- statement processing ------------------------------------------------

def process_statement_csv(
    *,
    database_url: str,
    csv_path: str,
    bank_name: str,
    auto_approve_under_cents: int = 10_000,
) -> dict:
    """Parse statement CSV, upsert each row into `transactions`.

    Returns counters: {inserted, updated, large_pending}.
    Rows with amount >= `auto_approve_under_cents` are inserted with
    status='pending_approval' (banking workflow picks them up).
    """
    inserted = 0
    updated = 0
    large_pending = 0
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ext_id = row["id"]
                posted = row["posted_date"]
                amount_cents = int(round(float(row["amount"]) * 100))
                merchant = row.get("merchant")
                category = _categorize(merchant, amount_cents)
                if abs(amount_cents) >= auto_approve_under_cents:
                    status = "pending_approval"
                    large_pending += 1
                else:
                    status = "committed"
                cur.execute(
                    """
                    INSERT INTO transactions
                        (external_id, posted_date, amount_cents, currency,
                         merchant, category, status, raw_row)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (external_id) DO UPDATE SET
                        posted_date = EXCLUDED.posted_date,
                        amount_cents = EXCLUDED.amount_cents,
                        merchant = EXCLUDED.merchant,
                        category = EXCLUDED.category,
                        raw_row = EXCLUDED.raw_row
                    RETURNING xmax = 0 AS inserted
                    """,
                    (
                        ext_id, posted, amount_cents, row.get("currency", "USD"),
                        merchant, category, status, json.dumps(row),
                    ),
                )
                was_inserted = cur.fetchone()[0]
                if was_inserted:
                    inserted += 1
                else:
                    updated += 1
        conn.commit()
    return {"inserted": inserted, "updated": updated, "large_pending": large_pending}


def _categorize(merchant: str | None, amount_cents: int) -> str:
    if not merchant:
        return "uncategorized"
    m = merchant.lower()
    if any(k in m for k in ("grocery", "market", "trader")):
        return "groceries"
    if any(k in m for k in ("uber", "lyft", "transit", "metro")):
        return "transport"
    if any(k in m for k in ("amazon", "store", "shop")):
        return "shopping"
    if any(k in m for k in ("restaurant", "cafe", "coffee", "bar")):
        return "dining"
    if any(k in m for k in ("rent", "landlord", "mortgage")):
        return "housing"
    if amount_cents > 0:
        return "income"
    return "uncategorized"
