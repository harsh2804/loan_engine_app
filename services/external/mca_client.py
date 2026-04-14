"""
Attestr GSTIN-to-CIN public lookup client.

POST /api/v2/public/lookup/gstin-to-cin
  Body: {"gstin": "..."}
  Returns records[0].reg (CIN) and records[0].incorporatedDate (DOI).
"""
from __future__ import annotations

from typing import Optional

from config.settings import get_settings
from services.external.base_client import ApiResponse, AuditCallback, BaseApiClient


class McaGstinClient(BaseApiClient):
    ENDPOINT = "/api/v2/public/lookup/gstin-to-cin"

    @property
    def base_url(self) -> str:
        return get_settings().attestr_public_base_url

    @property
    def service_name(self) -> str:
        return "MCA_GSTIN_TO_CIN"

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
        headers = {
            "Content-Type": "application/json"
        }

        # This endpoint expects Basic auth. Prefer dedicated basic token.
        settings = get_settings()
        token = (
            (settings.attestr_basic_auth_token or "").strip()
            or (settings.attestr_auth_token or "").strip()
        )
        if token:
            if token.lower().startswith(("basic ", "bearer ")):
                headers["Authorization"] = token
            elif token.count(".") == 2:
                # JWT-like token: keep backward compatibility for existing env setup.
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["Authorization"] = f"Basic {token}"

        return headers

    async def fetch_company_identity(
        self,
        *,
        gstin: str,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {"gstin": gstin.upper()}
        return await self._post(self.ENDPOINT, body, application_id=application_id)
