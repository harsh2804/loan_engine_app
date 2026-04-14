"""
utils/cibil_parser.py
──────────────────────
Parses the Surepass CIBIL credit-report JSON into a flat dict
consumed by the engine and lender rules.

Handles both:
  - Full Surepass structure (credit_report array with accounts/scores)
  - Minimal structure (score only)
"""
from __future__ import annotations
from datetime import datetime, timedelta, UTC
from typing import Any, Optional


def parse_cibil_payload(raw: dict) -> dict:
    """
    Entry point.  Returns a normalised dict with keys matching
    CibilSummarySchema fields + `raw_accounts` for downstream use.
    """
    # Unwrap Surepass envelope
    data    = raw.get("data", raw)
    reports = data.get("credit_report") or []

    # Score may be at the top level or inside the first report's scores array
    score = _extract_score(data, reports)

    if not reports:
        # Minimal payload — score only
        return _minimal_summary(data, score)

    report    = reports[0]
    accounts  = report.get("accounts") or []
    enquiries = report.get("enquiries") or []
    consumer_summary = (((report.get("response") or {}).get("consumerSummaryresp")) or {})
    account_summary = consumer_summary.get("accountSummary") or {}
    inquiry_summary = consumer_summary.get("inquirySummary") or {}

    # Active loans: open (dateClosed == "NA") with positive balance
    active_loans = [
        a for a in accounts
        if str(a.get("dateClosed", "")).upper() == "NA"
        and _to_float(a.get("currentBalance")) > 0
    ]

    # Total EMI from active loans that declare a valid emiAmount (>0)
    total_emi = sum(
        _to_float(a.get("emiAmount"))
        for a in active_loans
        if _to_float(a.get("emiAmount")) > 0
    )

    overdue_total   = sum(_to_float(a.get("amountOverdue")) for a in accounts)
    if account_summary.get("overdueBalance") not in (None, "", "-1"):
        overdue_total = _to_float(account_summary.get("overdueBalance"))
    max_days        = _compute_max_overdue_days(accounts)
    has_written_off = _check_written_off(accounts)
    recent_enq      = _count_recent_enquiries(enquiries, days=90)
    enquiries_2m    = _count_recent_enquiries(enquiries, days=60)
    enquiries_6m    = _count_recent_enquiries(enquiries, days=180)
    if not enquiries_6m:
        enquiries_6m = _summary_int(inquiry_summary, "inquiryPast12Months") or enquiries_6m
    active_unsecured = _count_active_unsecured_loans(active_loans)
    max_unsecured_loan_outstanding = _max_unsecured_loan_outstanding(active_loans)
    bounce_6m, bounce_12m = _count_emi_bounces(accounts)
    delinquency_last_12m = _has_recent_delinquency(accounts, days=365)
    unsecured_track_emi_count = _clean_unsecured_emi_count(accounts)

    return {
        "borrower_name":        data.get("name") or "",
        "pan":                  data.get("pan")  or "",
        "score":                score,
        "overdue_amount":       round(overdue_total, 2),
        "max_days_overdue":     max_days,
        "active_loan_count":    len(active_loans),
        "active_unsecured_loans": active_unsecured,
        "total_emi_from_cibil": round(total_emi, 2),
        "has_written_off":      has_written_off,
        "recent_enquiries_90d": recent_enq,
        "enquiries_last_2m":    enquiries_2m,
        "enquiry_count_6m":     enquiries_6m,
        "emi_bounce_last_6m":   bounce_6m,
        "bounce_count_12m":     bounce_12m,
        "delinquency_last_12m": delinquency_last_12m,
        "max_unsecured_loan_outstanding": round(max_unsecured_loan_outstanding, 2),
        "unsecured_track_emi_count": unsecured_track_emi_count,
        "raw_summary":          consumer_summary,
        "raw_accounts":         accounts,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_score(data: dict, reports: list) -> int:
    """Try multiple locations for the CIBIL score."""
    # Top-level credit_score field (Surepass shortcut)
    if data.get("credit_score"):
        try:
            return int(data["credit_score"])
        except (ValueError, TypeError):
            pass

    # Inside first report's scores array
    if reports:
        for score_obj in reports[0].get("scores", []):
            try:
                return int(score_obj["score"])
            except (KeyError, ValueError, TypeError):
                pass

    return 0


def _minimal_summary(data: dict, score: int) -> dict:
    return {
        "borrower_name":        data.get("name") or "",
        "pan":                  data.get("pan")  or "",
        "score":                score,
        "overdue_amount":       0.0,
        "max_days_overdue":     0,
        "active_loan_count":    0,
        "active_unsecured_loans": 0,
        "total_emi_from_cibil": 0.0,
        "has_written_off":      False,
        "recent_enquiries_90d": 0,
        "enquiries_last_2m":    0,
        "enquiry_count_6m":     0,
        "emi_bounce_last_6m":   0,
        "bounce_count_12m":     0,
        "delinquency_last_12m": False,
        "max_unsecured_loan_outstanding": 0.0,
        "unsecured_track_emi_count": 0,
        "raw_summary":          {},
        "raw_accounts":         [],
    }


def _compute_max_overdue_days(accounts: list[dict]) -> int:
    """
    Interprets the worst monthly pay status across all accounts.
    Status codes:
      0 = current
      1 = 1-29 days late   → 30d
      2 = 30-59 days late  → 60d
      3 = 60-89 days late  → 90d
      4 = 90-119           → 120d
      5 = 120-149          → 150d
      6 = 150+             → 180d
    """
    max_status = 0
    for acct in accounts:
        for mps in acct.get("monthlyPayStatus", []):
            raw_status = mps.get("status", "0")
            try:
                s = int(raw_status)
                if s > max_status:
                    max_status = s
            except (ValueError, TypeError):
                pass
    return max_status * 30


def _check_written_off(accounts: list[dict]) -> bool:
    """
    Returns True if any account has a non-empty
    suitFiledWillfulDefaultWrittenOff value.
    """
    for acct in accounts:
        val = str(acct.get("suitFiledWillfulDefaultWrittenOff") or "").strip()
        if val and val.upper() not in ("", "NA", "N", "NO", "NONE"):
            return True
    return False


def _count_recent_enquiries(enquiries: list[dict], *, days: int) -> int:
    """Count enquiries within the last N days."""
    cutoff = datetime.now(UTC).replace(tzinfo=None)
    count  = 0
    for enq in enquiries:
        date_str = enq.get("enquiryDate") or ""
        try:
            enq_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
            if (cutoff - enq_date).days <= days:
                count += 1
        except ValueError:
            pass
    return count


def _count_active_unsecured_loans(accounts: list[dict]) -> int:
    return sum(1 for acct in accounts if _is_unsecured(acct))


def _max_unsecured_loan_outstanding(accounts: list[dict]) -> float:
    unsecured_balances = [
        _to_float(acct.get("currentBalance"))
        for acct in accounts
        if _is_unsecured(acct)
    ]
    return max(unsecured_balances, default=0.0)


def _count_emi_bounces(accounts: list[dict]) -> tuple[int, int]:
    cutoff_6m = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=180)
    cutoff_12m = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365)
    bounce_6m = 0
    bounce_12m = 0
    for acct in accounts:
        for mps in acct.get("monthlyPayStatus", []):
            raw_status = str(mps.get("status", "")).strip().upper()
            try:
                status_value = int(raw_status)
            except ValueError:
                status_value = None
            if status_value is not None and status_value <= 0:
                continue
            if raw_status in {"0", "STD", "", "XXX"}:
                continue
            dt = _parse_status_date(mps.get("date"))
            if not dt:
                continue
            if dt >= cutoff_12m:
                bounce_12m += 1
            if dt >= cutoff_6m:
                bounce_6m += 1
    return bounce_6m, bounce_12m


def _has_recent_delinquency(accounts: list[dict], *, days: int) -> bool:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    for acct in accounts:
        for mps in acct.get("monthlyPayStatus", []):
            raw_status = str(mps.get("status", "")).strip().upper()
            try:
                status_value = int(raw_status)
            except ValueError:
                status_value = None
            if status_value is not None and status_value <= 0:
                continue
            if raw_status in {"0", "STD", "", "XXX"}:
                continue
            dt = _parse_status_date(mps.get("date"))
            if dt and dt >= cutoff:
                return True
    return False


def _clean_unsecured_emi_count(accounts: list[dict]) -> int:
    count = 0
    for acct in accounts:
        if not _is_unsecured(acct):
            continue
        for mps in acct.get("monthlyPayStatus", []):
            raw_status = str(mps.get("status", "")).strip().upper()
            if raw_status in {"0", "STD"}:
                count += 1
    return count


def _parse_status_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt)
        except ValueError:
            continue
    return None


def _summary_int(summary: dict, key: str) -> int:
    try:
        return int(str(summary.get(key) or "").strip())
    except ValueError:
        return 0


def _is_unsecured(account: dict) -> bool:
    account_type = str(account.get("accountType") or "").strip().lower()
    unsecured_keywords = (
        "personal loan",
        "business loan",
        "consumer loan",
        "credit card",
        "overdraft",
        "unsecured",
    )
    return any(keyword in account_type for keyword in unsecured_keywords)


def _to_float(value: Any) -> float:
    """Safely convert any value to float; return 0.0 on failure."""
    try:
        f = float(value)
        return 0.0 if f < 0 else f   # -1 is Surepass's "not available" sentinel
    except (ValueError, TypeError):
        return 0.0
