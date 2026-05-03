"""
services/existing_emi.py
────────────────────────
Existing EMI calculation per Pre-Screening Engine v1.2:

  Existing EMI = matched EMI (bureau + bank counted once)
               + unmatched bureau EMIs
               + unmatched bank EMIs

Matching rule (v1.2):
  - Lender name matches (normalized)
  - EMI amount difference ≤ 5%

When a match exists, we keep the bank-statement EMI amount (most recent actual
debit) and do not double-count the bureau EMI.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class EmiItem:
    source: str  # "bank" | "bureau"
    lender: str
    amount: float

    @property
    def lender_norm(self) -> str:
        return normalize_lender_name(self.lender)


@dataclass(frozen=True)
class ExistingEmiResult:
    total: float
    matched: tuple[tuple[EmiItem, EmiItem], ...]
    unmatched_bank: tuple[EmiItem, ...]
    unmatched_bureau: tuple[EmiItem, ...]


def normalize_lender_name(value: Optional[str]) -> str:
    """
    Normalizes lender names for loose matching across sources.
    Example: "HDFC Bank" → "HDFCBANK"
    """
    raw = (value or "").strip().upper()
    if not raw:
        return "UNKNOWN"
    return re.sub(r"[^A-Z0-9]+", "", raw) or "UNKNOWN"


def bank_emi_items_from_transactions(
    emi_txns: Iterable[dict[str, Any]],
    cls_index: dict[str, dict[str, Any]],
) -> list[EmiItem]:
    """
    Deduplicates recurring EMI debits into monthly EMI obligations by unique
    (lender, rounded amount) pairs, and returns the items for downstream
    bureau+bank deduplication.
    """
    items: list[EmiItem] = []
    seen: set[tuple[str, int]] = set()
    for t in emi_txns:
        lender = (cls_index.get(t.get("transaction_id")) or {}).get("emi_lender") or "UNKNOWN"
        try:
            amount = float(t.get("amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        key = (normalize_lender_name(lender), int(round(amount)))
        if key in seen or amount <= 0:
            continue
        seen.add(key)
        items.append(EmiItem(source="bank", lender=str(lender), amount=amount))
    return items


def bureau_emi_items_from_cibil_accounts(accounts: Iterable[dict[str, Any]]) -> list[EmiItem]:
    """
    Extracts active-loan EMI obligations from CIBIL account rows.
    This is intentionally defensive because upstream structures vary.
    """
    items: list[EmiItem] = []
    seen: set[tuple[str, int]] = set()

    for acct in accounts:
        date_closed = str(acct.get("dateClosed", "")).strip().upper()
        if date_closed != "NA":
            continue

        current_balance = _to_float(acct.get("currentBalance"))
        if current_balance <= 0:
            continue

        emi = _to_float(acct.get("emiAmount"))
        if emi <= 0:
            continue

        lender = _first_string(
            acct,
            {
                "subscriberName",
                "subscriber",
                "memberName",
                "member",
                "bankName",
                "lenderName",
                "institutionName",
                "financialInstitution",
                "grantorName",
            },
        ) or str(acct.get("accountType") or "UNKNOWN")

        key = (normalize_lender_name(lender), int(round(emi)))
        if key in seen:
            continue
        seen.add(key)
        items.append(EmiItem(source="bureau", lender=str(lender), amount=float(emi)))

    return items


def compute_existing_emi(
    *,
    bureau_items: Iterable[EmiItem],
    bank_items: Iterable[EmiItem],
    tolerance_pct: float = 0.05,
) -> ExistingEmiResult:
    """
    Combines bureau + bank EMIs with the v1.2 dedup rule.
    """
    bureau_list = list(bureau_items)
    bank_list = list(bank_items)

    remaining_bureau: list[Optional[EmiItem]] = bureau_list[:]
    matched: list[tuple[EmiItem, EmiItem]] = []
    unmatched_bank: list[EmiItem] = []

    for bank in bank_list:
        best_idx: Optional[int] = None
        best_diff: float = 10.0
        for idx, bureau in enumerate(remaining_bureau):
            if bureau is None:
                continue
            if bank.lender_norm != bureau.lender_norm:
                continue
            if bureau.amount <= 0:
                continue

            diff = abs(bank.amount - bureau.amount) / bureau.amount
            if diff <= tolerance_pct and diff < best_diff:
                best_diff = diff
                best_idx = idx

        if best_idx is None:
            unmatched_bank.append(bank)
            continue

        bureau = remaining_bureau[best_idx]
        if bureau is None:
            unmatched_bank.append(bank)
            continue

        matched.append((bank, bureau))
        remaining_bureau[best_idx] = None

    unmatched_bureau = [b for b in remaining_bureau if b is not None]

    total = (
        sum(bank.amount for bank, _ in matched)
        + sum(i.amount for i in unmatched_bank)
        + sum(i.amount for i in unmatched_bureau)
    )

    return ExistingEmiResult(
        total=round(float(total), 2),
        matched=tuple(matched),
        unmatched_bank=tuple(unmatched_bank),
        unmatched_bureau=tuple(unmatched_bureau),
    )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_string(d: dict[str, Any], keys: set[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None

