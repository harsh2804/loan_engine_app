"""
services/external/base_client.py
──────────────────────────────────
Abstract base for all outbound HTTP API clients.

Provides:
  - Retry with exponential backoff
  - Automatic duration measurement
  - Structured error wrapping
  - Pluggable audit logging via callback

All Surepass clients inherit from this.
"""
from __future__ import annotations
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

import httpx

from config.settings import get_settings


@dataclass
class ApiResponse:
    """Normalised response from any external API."""
    success:      bool
    status_code:  int
    data:         Optional[dict]
    error:        Optional[str]
    duration_ms:  float
    attempt:      int


# Audit callback type: async callable that receives request/response details
AuditCallback = Callable[..., Coroutine[Any, Any, None]]


class BaseApiClient(ABC):
    """
    Abstract base with retry, timing, and audit hooks.

    Subclasses implement _build_headers() and call _post() / _get().
    """

    def __init__(self, audit_callback: Optional[AuditCallback] = None) -> None:
        self._settings = get_settings()
        self._audit_cb = audit_callback

    @property
    def timeout_seconds(self) -> int:
        return self._settings.surepass_timeout_seconds

    @property
    def max_retries(self) -> int:
        return self._settings.surepass_max_retries

    @property
    def retry_backoff(self) -> float:
        return self._settings.surepass_retry_backoff

    # ── To be implemented by subclasses ──────────────────────────────────────

    @abstractmethod
    def _build_headers(self) -> dict[str, str]:
        """Return headers dict for every request (auth, content-type, etc.)."""
        ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        ...

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Short name used in audit logs: CIBIL | AA_INIT | AA_FETCH | CLAUDE"""
        ...

    # ── Core HTTP ─────────────────────────────────────────────────────────────

    async def _post(
        self,
        path: str,
        body: dict,
        *,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        return await self._request("POST", path, body=body, application_id=application_id)

    async def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        return await self._request("GET", path, params=params, application_id=application_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict]   = None,
        params: Optional[dict] = None,
        application_id: Optional[str] = None,
    ) -> ApiResponse:
        url      = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers  = self._build_headers()
        last_err: Optional[Exception] = None
        response: Optional[ApiResponse] = None

        for attempt in range(1, self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    if method == "POST":
                        http_resp = await client.post(url, headers=headers, json=body)
                    else:
                        http_resp = await client.get(url, headers=headers, params=params)

                duration_ms = (time.perf_counter() - t0) * 1000
                ok          = http_resp.status_code < 400

                try:
                    resp_json = http_resp.json()
                except Exception:
                    resp_json = {"raw": http_resp.text}

                response = ApiResponse(
                    success=ok,
                    status_code=http_resp.status_code,
                    data=resp_json if ok else None,
                    error=None if ok else str(resp_json),
                    duration_ms=duration_ms,
                    attempt=attempt,
                )

                await self._fire_audit(
                    application_id=application_id,
                    endpoint=url,
                    method=method,
                    request_body=body,
                    response_body=resp_json,
                    status_code=http_resp.status_code,
                    duration_ms=duration_ms,
                    success=ok,
                    attempt_number=attempt,
                )

                if ok:
                    return response

                # 4xx are not retryable
                if 400 <= http_resp.status_code < 500:
                    return response

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                duration_ms = (time.perf_counter() - t0) * 1000
                last_err = exc
                await self._fire_audit(
                    application_id=application_id,
                    endpoint=url,
                    method=method,
                    request_body=body,
                    status_code=None,
                    duration_ms=duration_ms,
                    success=False,
                    error_message=str(exc),
                    attempt_number=attempt,
                )

            if attempt < self.max_retries:
                backoff = self.retry_backoff * (2 ** (attempt - 1))
                await asyncio.sleep(backoff)

        # All retries exhausted: if we received at least one HTTP response,
        # return that final response (with real status/error) instead of
        # a generic transport-failure wrapper.
        if response is not None:
            return response

        return ApiResponse(
            success=False,
            status_code=0,
            data=None,
            error=f"All {self.max_retries} attempts failed: {last_err}",
            duration_ms=0,
            attempt=self.max_retries,
        )

    async def _fire_audit(self, **kwargs: Any) -> None:
        if self._audit_cb:
            try:
                await self._audit_cb(service=self.service_name, **kwargs)
            except Exception:
                pass  # Never let audit failure break the main flow
