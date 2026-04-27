"""
services/external/aa_client.py
──────────────────────────────
Surepass Account Aggregator v2 client.

User-driven AA flow (no server-side polling):
  Step 1 — POST /api/v1/account-aggregator-v2/init
    → returns client_id (and sometimes redirect_url)

  Step 2 — POST /api/v1/account-aggregator-v2/fetch-json-report
    → returns bank statement JSON once the user completes AA sign-in/consent in their banking app

Both calls use the same JWT token but a dedicated base URL.
"""

from __future__ import annotations

from typing import Optional

from config.settings import get_settings
from services.external.base_client import ApiResponse, AuditCallback, BaseApiClient


class AccountAggregatorClient(BaseApiClient):
    """
    Manages the AA init → fetch lifecycle.

    Important: This client intentionally does not poll for AA completion. The
    caller should invoke fetch only after the user explicitly completes the
    AA sign-in/consent flow in their banking app.
    """

    INIT_ENDPOINT = "/api/v1/account-aggregator-v2/init"
    FETCH_ENDPOINT = "/api/v1/account-aggregator-v2/fetch-json-report"

    def __init__(self, audit_callback: Optional[AuditCallback] = None) -> None:
        super().__init__(audit_callback)

    @property
    def base_url(self) -> str:
        return get_settings().surepass_aa_base_url

    @property
    def service_name(self) -> str:
        return "AA_INIT"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {get_settings().surepass_jwt_token}",
            "Content-Type": "application/json",
        }

    async def init_session(
        self,
        *,
        mobile_number: str,
        pan_number: str = "",
        email: str = "",
        input_redirect_url: str = "",
        consent_type: str = "loan_underwriting",
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {
            "mobile_number": mobile_number,
            "pan_number": pan_number,
            "email": email,
            "input_redirect_url": input_redirect_url,
            "consent_type": consent_type,
        }
        return await self._post(
            self.INIT_ENDPOINT,
            body,
            application_id=application_id,
        )

    async def fetch_report(
        self,
        *,
        client_id: str,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {"client_id": client_id}
        return await self._post(
            self.FETCH_ENDPOINT,
            body,
            application_id=application_id,
        )

    @staticmethod
    def extract_client_id(init_response: ApiResponse) -> Optional[str]:
        if not init_response.success or not init_response.data:
            return None
        data = init_response.data
        return data.get("client_id") or data.get("data", {}).get("client_id")

