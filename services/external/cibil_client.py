"""
services/external/cibil_client.py
──────────────────────────────────
Surepass CIBIL API client.

POST /api/v1/credit-report-cibil/fetch-report
  Body: mobile, pan, name, gender, consent
  Returns: full CIBIL credit report JSON
"""
from __future__ import annotations
from typing import Optional

from services.external.base_client import BaseApiClient, ApiResponse, AuditCallback
from config.settings import get_settings


class CibilClient(BaseApiClient):
    """
    Single responsibility: call the Surepass CIBIL endpoint and return raw JSON.
    """

    ENDPOINT = "/api/v1/credit-report-cibil/fetch-report"

    def __init__(self, audit_callback: Optional[AuditCallback] = None) -> None:
        super().__init__(audit_callback)

    @property
    def base_url(self) -> str:
        return get_settings().surepass_base_url

    @property
    def service_name(self) -> str:
        return "CIBIL"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {get_settings().surepass_jwt_token}",
            "Content-Type":  "application/json",
        }

    async def fetch_report(
        self,
        *,
        mobile: str,
        pan: str,
        name: str,
        gender: str = "male",
        consent: str = "Y",
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        """
        Fetch the full CIBIL report for a borrower.

        Returns ApiResponse.data = raw Surepass JSON on success.
        """
        normalized_gender = _normalize_cibil_gender(gender)
        body = {
            "mobile":  mobile,
            "pan":     pan.upper(),
            "name":    name,
            "gender":  normalized_gender,
            "consent": consent,
        }
        print("body is ", body)
        return await self._post(
            self.ENDPOINT,
            body,
            application_id=application_id,
        )


def _normalize_cibil_gender(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"male", "m"}:
        return "male"
    if raw in {"female", "f"}:
        return "female"
    # Surepass CIBIL rejects "other"; fallback keeps flow working.
    return "male"
