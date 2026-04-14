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
    ENDPOINT = "/verify-gst"

    @property
    def base_url(self) -> str:
        return get_settings().capaxis_base_url

    @property
    def service_name(self) -> str:
        return "GST_VERIFY"

    @property
    def timeout_seconds(self) -> int:
        return get_settings().capaxis_timeout_seconds

    @property
    def max_retries(self) -> int:
        return get_settings().capaxis_max_retries

    @property
    def retry_backoff(self) -> float:
        return get_settings().capaxis_retry_backoff

    def _build_headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "Content-Type": "application/json",
        }

    async def verify_gst(
        self,
        *,
        gstin: str,
        fetch_filings: bool = False,
        financial_year: Optional[str] = None,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {
            "gstin": gstin.upper(),
            "fetch_filings": fetch_filings,
            "financial_year": financial_year or "",
        }
        return await self._post(self.ENDPOINT, body, application_id=application_id)
