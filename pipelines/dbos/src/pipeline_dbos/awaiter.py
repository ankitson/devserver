"""DBOS implementation of the AsyncOtpAwaiter protocol."""

from __future__ import annotations

import logging

from dbos import DBOS

from pipeline_shared import Notifier, OtpRequest
from pipeline_shared.config import Settings

log = logging.getLogger(__name__)


class DbosOtpAwaiter:
    """Uses DBOS workflow events: `await DBOS.recv_async("otp")` inside the
    workflow, `DBOS.send(workflow_id, otp_value, topic="otp")` from a FastAPI
    handler. We capture the workflow_id from DBOS.workflow_id at construction
    so the notification can include it.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.wf_id = DBOS.workflow_id

    def workflow_id(self) -> str:
        return self.wf_id

    async def request_otp(self, req: OtpRequest) -> None:
        Notifier(self.settings).enqueue(
            kind="bank:otp_required",
            severity="warn",
            title=f"OTP required for {req.bank_name}",
            body=(
                f"Bank sent OTP to {req.masked_destination}. "
                f"Open {req.prompt_url} to enter it."
            ),
            payload={
                "workflow_id": req.workflow_id,
                "bank": req.bank_name,
                "masked_destination": req.masked_destination,
                "prompt_url": req.prompt_url,
            },
        )

    async def wait_for_otp(self, timeout_seconds: int) -> str:
        otp = await DBOS.recv_async("otp", timeout_seconds=timeout_seconds)
        if otp is None:
            raise TimeoutError("OTP not received within deadline")
        return str(otp).strip()
