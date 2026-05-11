"""
utils/usage_limits.py
─────────────────────
Pure helpers for monthly usage limits.

Rules (business requirements):
  - Limits reset on the 1st of every month (calendar months).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class MonthlyQuotaState:
    month: str | None  # "YYYY-MM"
    count: int


@dataclass(frozen=True)
class MonthlyQuotaResult:
    allowed: bool
    state: MonthlyQuotaState
    month_key: str
    next_reset_date: str  # "YYYY-MM-01"
    remaining: int


def month_key_utc(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def next_reset_date_utc(month_key: str) -> str:
    year_s, month_s = month_key.split("-", 1)
    year = int(year_s)
    month = int(month_s)
    # first day of next month
    if month == 12:
        return f"{year + 1:04d}-01-01"
    return f"{year:04d}-{month + 1:02d}-01"


def consume_monthly_quota(
    *,
    state: MonthlyQuotaState,
    limit: int,
    now: datetime | None = None,
) -> MonthlyQuotaResult:
    """
    Returns updated state if allowed, or same/reset state if denied.
    """
    if limit <= 0:
        # treat as disabled
        mk = month_key_utc(now)
        return MonthlyQuotaResult(
            allowed=True,
            state=MonthlyQuotaState(month=mk, count=0),
            month_key=mk,
            next_reset_date=next_reset_date_utc(mk),
            remaining=0,
        )

    mk = month_key_utc(now)
    current_month = state.month
    count = int(state.count or 0)

    if current_month != mk:
        count = 0
        current_month = mk

    allowed = count < limit
    new_count = count + 1 if allowed else count
    remaining = max(limit - new_count, 0) if allowed else 0

    new_state = MonthlyQuotaState(month=current_month, count=new_count)
    return MonthlyQuotaResult(
        allowed=allowed,
        state=new_state,
        month_key=mk,
        next_reset_date=next_reset_date_utc(mk),
        remaining=remaining,
    )

