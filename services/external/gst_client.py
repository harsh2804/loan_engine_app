"""
Capaxis GST verification client.

POST /verify-gst
  Body: gstin, fetch_filings, financial_year
  Returns GST registration details including PAN in the response payload.
"""
from __future__ import annotations

from typing import Optional

from config.settings import get_settings
from services.external.base_client import ApiResponse, AuditCallback, BaseApiClient


class GstVerificationClient(BaseApiClient):
    ENDPOINT = "api/v2/public/corpx/gstin"

    @property
    def base_url(self) -> str:
        return get_settings().attestr_public_base_url

    @property
    def service_name(self) -> str:
        return "GST_VERIFY"

    @property
    def timeout_seconds(self) -> int:
        return get_settings().attestr_timeout_seconds

    @property
    def max_retries(self) -> int:
        return get_settings().attestr_max_retries

    @property
    def retry_backoff(self) -> float:
        return get_settings().attestr_retry_backoff

    def _build_headers(self) -> dict[str, str]:
        settings = get_settings()
        token = (
            (settings.attestr_basic_auth_token or "").strip()
            or (settings.attestr_auth_token or "").strip()
        )
        headers = {
            "Content-Type": "application/json",
        }
        if token:
            if not token.lower().startswith("basic "):
                headers["Authorization"] = f"Basic {token}"
            else:
                headers["Authorization"] = token
        return headers

    async def verify_gst(
        self,
        *,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {
            "gstin": gstin.upper(),
            "fetchFilings": fetch_filings,
            "fy": fy,
        }
        return await self._post(self.ENDPOINT, body, application_id=application_id)
