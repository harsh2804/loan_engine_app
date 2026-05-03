"""
services/external/itr_client.py
───────────────────────────────
Attestr MSME ITR & Turnover by PAN API client (v2).

Docs (as referenced in Pre-Screening Engine v1.2 PDF):
  POST https://api.attestr.com/api/v2/public/corpx/itr
  Body: pan, birthOrIncorporatedDate (DD/MM/YYYY), name
  Response: grossTurnover (string) + formatting fields
"""

from __future__ import annotations

from typing import Optional

from config.settings import get_settings
from services.external.base_client import ApiResponse, AuditCallback, BaseApiClient


class ItrTurnoverClient(BaseApiClient):
    ENDPOINT = "api/v2/public/corpx/itr"

    def __init__(self, audit_callback: Optional[AuditCallback] = None) -> None:
        super().__init__(audit_callback)

    @property
    def base_url(self) -> str:
        return get_settings().attestr_public_base_url

    @property
    def service_name(self) -> str:
        return "ITR_TURNOVER"

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
        headers = {"Content-Type": "application/json"}
        if token:
            if not token.lower().startswith("basic "):
                headers["Authorization"] = f"Basic {token}"
            else:
                headers["Authorization"] = token
        return headers

    async def fetch_turnover(
        self,
        *,
        pan: str,
        birth_or_incorporated_date: str,
        name: str,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        body = {
            "pan": (pan or "").upper(),
            "birthOrIncorporatedDate": birth_or_incorporated_date,
            "name": name,
        }
        return await self._post(self.ENDPOINT, body, application_id=application_id)

