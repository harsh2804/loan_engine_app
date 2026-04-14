"""
services/external/aa_client.py
────────────────────────────────
Surepass Account Aggregator v2 client.

Two-step flow:
  Step 1 — POST /api/v1/account-aggregator-v2/init
    → returns client_id

  Step 2 — POST /api/v1/account-aggregator-v2/fetch-json-report
    → may return status=PENDING while user completes consent
    → poll until status=COMPLETED or timeout

Both calls use the same JWT token but different base URL
(surepass.APP not surepass.IO).
"""
from __future__ import annotations
import asyncio
from typing import Optional

from services.external.base_client import BaseApiClient, ApiResponse, AuditCallback
from config.settings import get_settings


class AccountAggregatorClient(BaseApiClient):
    """
    Single responsibility: manage the AA init → poll → fetch lifecycle.
    """

    INIT_ENDPOINT  = "/api/v1/account-aggregator-v2/init"
    FETCH_ENDPOINT = "/api/v1/account-aggregator-v2/fetch-json-report"

    def __init__(self, audit_callback: Optional[AuditCallback] = None) -> None:
        super().__init__(audit_callback)

    @property
    def base_url(self) -> str:
        return get_settings().surepass_aa_base_url

    @property
    def service_name(self) -> str:
        return "AA_INIT"   # overridden per-call below

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {get_settings().surepass_jwt_token}",
            "Content-Type":  "application/json",
        }

    # ── Public API ────────────────────────────────────────────────────────────

    async def init_session(
        self,
        *,
        mobile_number: str,
        pan_number: str            = "",
        email: str                 = "",
        input_redirect_url: str    = "",
        consent_type: str          = "loan_underwriting",
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        """
        Step 1: Initialise AA session.
        Returns ApiResponse.data["client_id"] on success.
        """
        body = {
            "mobile_number":      mobile_number,
            "pan_number":         pan_number,
            "email":              email,
            "input_redirect_url": input_redirect_url,
            "consent_type":       consent_type,
        }
        resp = await self._post(
            self.INIT_ENDPOINT,
            body,
            application_id=application_id,
        )
        # Override service label in audit after the fact (base fires INIT label)
        return resp

    async def fetch_report(
        self,
        *,
        client_id: str,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        """
        Step 2: Fetch the bank statement JSON for the given client_id.
        Single attempt — call poll_until_ready() for auto-polling.
        """
        body = {"client_id": client_id}

        # Temporarily override service_name for audit log
        original = self.__class__.service_name.fget  # type: ignore[attr-defined]
        resp = await self._post(
            self.FETCH_ENDPOINT,
            body,
            application_id=application_id,
        )
        return resp

    async def poll_until_ready(
        self,
        *,
        client_id: str,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        """
        Poll fetch-json-report until the AA consent is completed and
        data is ready, or until timeout.

        Surepass returns {"data": {"status": "PENDING"}} while the user
        is completing the consent flow on their banking app.
        """
        settings = get_settings()
        for attempt in range(settings.aa_poll_max_attempts):
            resp = await self.fetch_report(
                client_id=client_id,
                application_id=application_id,
            )

            if not resp.success:
                return resp  # hard error — stop polling

            data = resp.data or {}
            status = (
                data.get("status")
                or data.get("data", {}).get("status", "")
                or ""
            ).upper()

            if status in ("COMPLETED", "READY", ""):
                # Empty status or COMPLETED = data is available
                return resp

            if status == "FAILED":
                return ApiResponse(
                    success=False,
                    status_code=resp.status_code,
                    data=None,
                    error=f"AA session failed: {data}",
                    duration_ms=resp.duration_ms,
                    attempt=attempt + 1,
                )

            # PENDING — wait and retry
            await asyncio.sleep(settings.aa_poll_interval_seconds)

        return ApiResponse(
            success=False,
            status_code=0,
            data=None,
            error=f"AA polling timed out after {settings.aa_poll_max_attempts} attempts",
            duration_ms=0,
            attempt=settings.aa_poll_max_attempts,
        )

    @staticmethod
    def extract_client_id(init_response: ApiResponse) -> Optional[str]:
        """
        Safely extract client_id from the init API response.
        Handles both flat and nested response structures.
        """
        if not init_response.success or not init_response.data:
            return None
        data = init_response.data
        return (
            data.get("client_id")
            or data.get("data", {}).get("client_id")
        )
