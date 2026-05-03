"""
utils/itr_parser.py
───────────────────
Parses Attestr ITR/turnover API response into a minimal usable dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ItrTurnoverSummary:
    valid: bool
    fy: Optional[str]
    itr_filed: Optional[bool]
    itr_type: Optional[str]
    gross_turnover_annual: Optional[float]
    pan_status: Optional[str]
    message: Optional[str]


def parse_itr_turnover_payload(raw: Optional[dict[str, Any]]) -> ItrTurnoverSummary:
    if not raw or not isinstance(raw, dict):
        return ItrTurnoverSummary(
            valid=False,
            fy=None,
            itr_filed=None,
            itr_type=None,
            gross_turnover_annual=None,
            pan_status=None,
            message="Missing/invalid ITR payload",
        )

    valid = bool(raw.get("valid") is True)
    gross = _to_float(raw.get("grossTurnover"))

    return ItrTurnoverSummary(
        valid=valid,
        fy=_to_str(raw.get("fy")),
        itr_filed=_to_bool(raw.get("itrFiled")),
        itr_type=_to_str(raw.get("itrType")),
        gross_turnover_annual=gross if gross > 0 else None,
        pan_status=_to_str(raw.get("panStatus")),
        message=_to_str(raw.get("message")),
    )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"true", "1", "y", "yes"}:
        return True
    if s in {"false", "0", "n", "no"}:
        return False
    return None

