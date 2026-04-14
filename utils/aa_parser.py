"""
utils/aa_parser.py
──────────────────
Cleans and normalises the raw Surepass Account Aggregator v2 JSON.

Known data-quality issues handled:
  1. Duplicate transaction_ids           → composite-key dedup
  2. Missing closing balances            → carry-forward
  3. Mixed amount types (str/float/int)  → cast to float
  4. Timezone noise in timestamps        → strip to YYYY-MM-DD
  5. Nested vs flat response shapes      → normalised access
"""
from __future__ import annotations
import hashlib
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Optional


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_aa_payload(raw: dict) -> dict:
    """
    Accepts the raw Surepass AA JSON (any nesting level).
    Returns a normalised dict with:
      - account_id, account_number
      - profile:      borrower KYC fields
      - summary:      account metadata
      - transactions: list[dict] — deduplicated, sorted ascending
    """
    # Unwrap common nesting patterns from Surepass
    data = raw.get("data", raw)

    accounts: list[dict] = (
        data.get("account_aggregator_json")
        or data.get("accounts")
        or []
    )

    if not accounts:
        # Try flat response where transaction_details is at root level
        if "transaction_details" in data:
            accounts = [{"transaction_data": {"transaction_details": data["transaction_details"]}}]
        else:
            raise ValueError("No account data found in AA payload")

    acct    = _pick_primary_account(accounts)
    profile = acct.get("profile_details") or acct.get("profile") or {}
    summary = acct.get("summary_details") or acct.get("summary") or {}
    raw_txns = (
        acct.get("transaction_data", {}).get("transaction_details")
        or acct.get("transactions", [])
    )

    return {
        "account_id":     acct.get("account_id", ""),
        "account_number": (
            acct.get("fi_status_details", {}).get("account_number")
            or summary.get("account_number", "")
        ),
        "accounts":       accounts,
        "profile":        profile,
        "summary":        summary,
        "transactions":   _clean_transactions(raw_txns),
    }


# ── Account selection ─────────────────────────────────────────────────────────

def _pick_primary_account(accounts: list[dict]) -> dict:
    """
    Prefer an ACTIVE CURRENT account, then ACTIVE SAVINGS.
    Fall back to any ACTIVE account, then first account.
    """
    for acct in accounts:
        s = acct.get("summary_details") or acct.get("summary") or {}
        if (
            str(s.get("status", "")).upper() == "ACTIVE"
            and str(s.get("account_sub_type", "")).upper() == "CURRENT"
        ):
            return acct

    for acct in accounts:
        s = acct.get("summary_details") or acct.get("summary") or {}
        if (
            str(s.get("status", "")).upper() == "ACTIVE"
            and str(s.get("account_sub_type", "")).upper() == "SAVINGS"
        ):
            return acct

    for acct in accounts:
        s = acct.get("summary_details") or acct.get("summary") or {}
        if str(s.get("status", "")).upper() == "ACTIVE":
            return acct

    return accounts[0]


# ── Transaction cleaning ──────────────────────────────────────────────────────

def _clean_transactions(raw_txns: list[dict]) -> list[dict]:
    """
    1. Cast amount & balance to float.
    2. Normalise timestamp → YYYY-MM-DD.
    3. Composite dedup (not on transaction_id alone — AA has duplicate IDs).
    4. Sort ascending by date.
    """
    seen:    set[str]  = set()
    cleaned: list[dict] = []

    for t in raw_txns:
        try:
            amount  = float(t.get("amount") or 0)
            balance = float(t.get("transaction_balance") or 0)
        except (ValueError, TypeError):
            continue

        ts_raw   = t.get("transaction_timestamp") or t.get("value_date") or ""
        txn_date = _parse_date(ts_raw)
        narration = (t.get("narration") or "").strip()
        txn_type  = (t.get("type") or "").upper()

        # Composite dedup key: amount + narration + type + date
        dedup_key = hashlib.md5(
            f"{amount}|{narration}|{txn_type}|{txn_date}".encode()
        ).hexdigest()

        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        cleaned.append({
            "transaction_id":      t.get("transaction_id", ""),
            "amount":              amount,
            "narration":           narration,
            "type":                txn_type,      # "CREDIT" | "DEBIT"
            "mode":                (t.get("mode") or "").upper(),
            "transaction_date":    txn_date,       # "YYYY-MM-DD"
            "transaction_balance": balance,
        })

    cleaned.sort(key=lambda x: x["transaction_date"])
    return cleaned


def _parse_date(ts: str) -> str:
    """Return YYYY-MM-DD from any timestamp string."""
    ts = str(ts).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(ts[:26], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ts[:10] if len(ts) >= 10 else "1970-01-01"


# ── Monthly credit aggregation ────────────────────────────────────────────────

def aggregate_monthly_credits(
    transactions: list[dict],
    classified_credits: list[dict],
) -> dict[str, float]:
    """
    Aggregate monthly Revenue credits only.
    Falls back to all credits if no classifications provided.

    classified_credits: list of {transaction_id, credit_category}
    Returns {YYYY-MM: total_amount}.
    """
    revenue_ids: set[str] = {
        c["transaction_id"]
        for c in classified_credits
        if c.get("credit_category") == "Revenue"
    } if classified_credits else set()

    monthly: dict[str, float] = defaultdict(float)
    for t in transactions:
        if t["type"] != "CREDIT":
            continue
        if revenue_ids and t["transaction_id"] not in revenue_ids:
            continue
        month_key = t["transaction_date"][:7]   # "YYYY-MM"
        monthly[month_key] += t["amount"]

    return dict(sorted(monthly.items()))


# ── Daily balance computation ─────────────────────────────────────────────────

def compute_daily_balances(transactions: list[dict]) -> dict[str, float]:
    """
    Build a complete daily closing-balance series.
    Missing days are filled by carrying forward the previous day's balance.
    Returns {YYYY-MM-DD: closing_balance}.
    """
    if not transactions:
        return {}

    # Last balance recorded per day
    daily_last: dict[str, float] = {}
    for t in transactions:
        d = t["transaction_date"]
        daily_last[d] = t["transaction_balance"]

    sorted_dates = sorted(daily_last)
    start = datetime.strptime(sorted_dates[0],  "%Y-%m-%d").date()
    end   = datetime.strptime(sorted_dates[-1], "%Y-%m-%d").date()

    result:       dict[str, float] = {}
    last_balance: float            = 0.0
    current:      date             = start

    while current <= end:
        key = current.strftime("%Y-%m-%d")
        if key in daily_last:
            last_balance = daily_last[key]
        result[key]  = last_balance
        current     += timedelta(days=1)

    return result


# ── Active-day counting ───────────────────────────────────────────────────────

def count_active_days(transactions: list[dict]) -> int:
    """Count distinct calendar days that had at least one credit transaction."""
    return len({
        t["transaction_date"]
        for t in transactions
        if t["type"] == "CREDIT"
    })
