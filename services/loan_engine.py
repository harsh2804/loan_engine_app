"""
services/loan_engine.py
────────────────────────
Pure-Python deterministic engine. Zero HTTP calls. Zero AI.

Implements every calculation from the Capaxis Pre-Screening PDF:

  BTO → μ (median) → σ → CV → QoQ → Revenue Concentration → ABB → ADR
  → Operating Buffer → Survival Surplus → Base Safe EMI
  → Penalties: Volatility / Concentration / Vintage / QoQ
  → Stress Test (30% revenue drop)
  → Final Safe EMI → Risk Band → Safe Loan Amount

Public API:
  run_engine(monthly_credits, daily_balances, active_days,
             existing_emi, business_vintage_months) → dict
  compute_monthly_emi_from_bank(emi_txns, cls_index) → float
"""
from __future__ import annotations
import math

DEFAULT_MONTHLY_INTEREST_RATE = 0.015 #0.00125
"""
PDF v1.2 documents "1.5% per month (fixed)", but the numeric examples in the
PDF match 1.5% per *year* (i.e. 0.125% per month = 0.00125).

We use the example-consistent monthly rate here.
"""


# =============================================================================
# Interpretation tables (from PDF)
# =============================================================================

def _volatility_interpretation(cv: float) -> str:
    if cv < 0.25:   return "Very stable"
    if cv < 0.45:   return "Normal MSME"
    if cv < 0.65:   return "Risky"
    return               "Dangerous"


def _concentration_interpretation(pct: float) -> str:
    if pct < 30:    return "Healthy diversification"
    if pct < 40:    return "Moderate dependency"
    if pct < 60:    return "Single-client risk"
    return               "Extreme fragility"


def _active_days_interpretation(ratio: float) -> str:
    if ratio > 0.70: return "Daily operating business"
    if ratio > 0.40: return "Semi-regular"
    return                "Seasonal / project-based"


# =============================================================================
# Public functions
# =============================================================================

def run_engine(
    monthly_credits:         dict[str, float],
    daily_balances:          dict[str, float],
    active_days:             int,
    existing_emi:            float,
    business_vintage_months: int,
) -> dict:
    """
    Master deterministic engine.
    Returns a flat dict of every intermediate and final value.
    All calculations exactly match the Capaxis Pre-Screening PDF.
    """
    if not monthly_credits:
        raise ValueError("No monthly credit data — cannot run engine")

    months  = sorted(monthly_credits.keys())
    last_12 = months[-12:]
    values  = [monthly_credits[m] for m in last_12]

    # ── Banking turnover ──────────────────────────────────────────────────────
    # BTO = Total credits (last 12 months) / 12
    total_inflow = sum(values)
    bto          = total_inflow / len(values)

    # ── Median monthly flow μ ─────────────────────────────────────────────────
    mu = _median(values)

    # ── Standard deviation σ ──────────────────────────────────────────────────
    sigma = _std_dev(values, mu)

    # ── QoQ growth ────────────────────────────────────────────────────────────
    # QoQ % = (Last quarter − Previous quarter) / Previous quarter
    qoq = _qoq(monthly_credits, months)   # decimal fraction

    # ── Volatility Index CV = σ / μ ───────────────────────────────────────────
    cv = sigma / mu if mu > 0 else 0.0

    # ── Revenue Concentration = Top3 credits / Total inflow ───────────────────
    top3_sum = sum(sorted(values, reverse=True)[:3])
    rev_conc = top3_sum / total_inflow if total_inflow > 0 else 0.0

    # ── Average Daily Bank Balance (ABB) ──────────────────────────────────────
    abb = sum(daily_balances.values()) / len(daily_balances) if daily_balances else 0.0

    # ── Active Days Ratio ─────────────────────────────────────────────────────
    total_days = max(len(daily_balances), 365)
    adr        = active_days / total_days

    # ── Operating Buffer = μ × 35% ────────────────────────────────────────────
    op_buffer = mu * 0.35

    # ── Survival Surplus = μ − Operating Buffer − Existing EMI ───────────────
    survival_surplus = mu - op_buffer - existing_emi

    # ── Base Safe EMI = Survival Surplus × 40% ────────────────────────────────
    base_safe_emi = survival_surplus * 0.40

    # ── Risk Penalties ────────────────────────────────────────────────────────
    vol_mult  = _volatility_multiplier(cv)
    conc_mult = _concentration_multiplier(rev_conc)
    vin_mult  = _vintage_multiplier(business_vintage_months)
    qoq_mult  = _qoq_multiplier(qoq)

    # Penalties are multiplicative — applied sequentially to base safe EMI
    emi_after_penalties = base_safe_emi * vol_mult * conc_mult * vin_mult * qoq_mult

    # ── Stress Test: 30% revenue drop ────────────────────────────────────────
    # Stressed Inflow = μ × (1 − 0.30) = μ × 0.70
    stress_inflow  = mu * 0.70
    stress_op_buf  = stress_inflow * 0.35
    stress_surplus = stress_inflow - stress_op_buf - existing_emi
    stress_emi     = stress_surplus * 0.40

    # ── Final Safe EMI = min(EMI after penalties, Stress EMI) ────────────────
    final_safe_emi = min(emi_after_penalties, stress_emi)

    # ── Risk Band + Tenure ────────────────────────────────────────────────────
    combined_mult     = vol_mult * conc_mult * vin_mult * qoq_mult
    risk_band, tenure = _risk_band(combined_mult)

    # ── Safe Loan Amount (principal) derived from EMI, rate, and tenure ──────
    # P = EMI × ((1+r)^n − 1) / (r × (1+r)^n)
    # (rounded to ₹10k for UI stability)
    safe_loan = _round_to(
        loan_amount_from_emi(
            final_safe_emi,
            tenure_months=tenure,
            monthly_interest_rate=DEFAULT_MONTHLY_INTEREST_RATE,
        ),
        10_000,
    )

    return {
        # ── Input echoes ──────────────────────────────────────────────────────
        "monthly_credits":               {m: monthly_credits[m] for m in last_12},
        "total_credit_inflow":           round(total_inflow, 2),
        "active_days":                   active_days,
        "detected_existing_emi":         round(existing_emi, 2),

        # ── Banking metrics ───────────────────────────────────────────────────
        "abb_daily":                     round(abb, 2),
        "bto_monthly_avg":               round(bto, 2),

        # ── Statistical analysis ──────────────────────────────────────────────
        "median_monthly_flow":           round(mu, 2),
        "std_dev":                       round(sigma, 2),
        "qoq_pct":                       round(qoq * 100, 2),
        "volatility_index":              round(cv, 4),
        "volatility_interpretation":     _volatility_interpretation(cv),
        "revenue_concentration_pct":     round(rev_conc * 100, 2),
        "concentration_interpretation":  _concentration_interpretation(rev_conc * 100),
        "active_days_ratio":             round(adr, 4),
        "active_days_interpretation":    _active_days_interpretation(adr),

        # ── EMI waterfall ─────────────────────────────────────────────────────
        "operating_buffer":              round(op_buffer, 2),
        "survival_surplus":              round(survival_surplus, 2),
        "base_safe_emi":                 round(base_safe_emi, 2),

        # ── Risk penalties ────────────────────────────────────────────────────
        "volatility_multiplier":         vol_mult,
        "concentration_multiplier":      conc_mult,
        "vintage_multiplier":            vin_mult,
        "qoq_multiplier":                qoq_mult,
        "combined_risk_multiplier":      round(combined_mult, 4),
        "emi_after_penalties":           round(emi_after_penalties, 2),

        # ── Stress test ───────────────────────────────────────────────────────
        "stress_inflow":                 round(stress_inflow, 2),
        "stress_operating_buffer":       round(stress_op_buf, 2),
        "stress_survival_surplus":       round(stress_surplus, 2),
        "stress_emi":                    round(stress_emi, 2),

        # ── Final decision ────────────────────────────────────────────────────
        "final_safe_emi":                round(final_safe_emi, 2),
        "risk_band":                     risk_band,
        "tenure_multiplier":             tenure,
        "safe_loan_amount":              safe_loan,
    }


def compute_monthly_emi_from_bank(
    emi_txns:  list[dict],
    cls_index: dict[str, dict],
) -> float:
    """
    Derive the monthly EMI obligation from bank-statement debits
    that Claude labelled as EMI obligations.

    Deduplication: each unique (lender, amount) pair is counted once.
    Recurring monthly EMIs appear multiple times in the statement —
    we want the monthly figure, not the sum across all months.
    """
    seen:  set[tuple] = set()
    total: float      = 0.0
    for t in emi_txns:
        lender = (cls_index.get(t["transaction_id"]) or {}).get("emi_lender") or "UNKNOWN"
        key    = (lender, round(t["amount"]))
        if key not in seen:
            seen.add(key)
            total += t["amount"]
    return total


def loan_amount_from_emi(
    emi: float,
    *,
    tenure_months: int,
    monthly_interest_rate: float = DEFAULT_MONTHLY_INTEREST_RATE,
) -> float:
    """
    Convert EMI → principal using standard amortization PV.
    Returns 0 for non-positive EMI or tenure.
    """
    if emi <= 0 or tenure_months <= 0:
        return 0.0
    r = monthly_interest_rate
    if r <= 0:
        return emi * tenure_months
    factor = (1 + r) ** tenure_months
    denom = r * factor
    if denom <= 0:
        return 0.0
    return emi * (factor - 1) / denom


def emi_from_loan_amount(
    principal: float,
    *,
    tenure_months: int,
    monthly_interest_rate: float = DEFAULT_MONTHLY_INTEREST_RATE,
) -> float:
    """
    Convert principal → EMI using standard amortization.
    Returns 0 for non-positive principal or tenure.
    """
    if principal <= 0 or tenure_months <= 0:
        return 0.0
    r = monthly_interest_rate
    if r <= 0:
        return principal / tenure_months
    factor = (1 + r) ** tenure_months
    denom = factor - 1
    if denom <= 0:
        return 0.0
    return principal * r * factor / denom


def banking_turnover_ratio_pct(
    *,
    monthly_banking_credit: float,
    annual_turnover: float | None,
) -> float | None:
    """
    Banking Turnover Ratio (v1.2):
      (Monthly Banking Credit / Monthly Turnover) * 100
    """
    if annual_turnover is None or annual_turnover <= 0:
        return None
    if monthly_banking_credit < 0:
        return None
    monthly_turnover = annual_turnover / 12
    if monthly_turnover <= 0:
        return None
    return (monthly_banking_credit / monthly_turnover) * 100


def requested_loan_risk_level(stress_ratio: float | None) -> str | None:
    """
    v1.2 "Your Original Request" classification:
      ratio ≤ 1.0        → Low Risk
      1.0 – 1.4          → Moderate Risk
      1.4 – 1.8 (and up) → High Risk
    """
    if stress_ratio is None:
        return None
    if stress_ratio <= 1.0:
        return "Low Risk"
    if stress_ratio <= 1.4:
        return "Moderate Risk"
    return "High Risk"


# =============================================================================
# Private helpers
# =============================================================================

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    sv  = sorted(values)
    n   = len(sv)
    mid = n // 2
    return (sv[mid - 1] + sv[mid]) / 2 if n % 2 == 0 else sv[mid]


def _std_dev(values: list[float], mu: float) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def _qoq(monthly_credits: dict[str, float], months: list[str]) -> float:
    """
    QoQ % = (Last quarter − Previous quarter) / Previous quarter
    Returns decimal fraction e.g. −0.028 = −2.8%
    """
    if len(months) < 6:
        return 0.0
    last_q = sum(monthly_credits.get(m, 0) for m in months[-3:])
    prev_q = sum(monthly_credits.get(m, 0) for m in months[-6:-3])
    return (last_q - prev_q) / prev_q if prev_q > 0 else 0.0


def _volatility_multiplier(cv: float) -> float:
    """Penalty multiplier from CV (Volatility Index) — PDF table."""
    if cv <= 0.25:  return 1.00   # Very stable  — no penalty
    if cv <= 0.45:  return 0.85   # Normal MSME
    if cv <= 0.65:  return 0.65   # Risky
    return                0.45   # Extremely unstable


def _concentration_multiplier(conc: float) -> float:
    """conc is 0–1 fraction. Penalty if top-3 > 40% of total."""
    return 0.70 if conc > 0.40 else 1.00


def _vintage_multiplier(months: int) -> float:
    """Vintage < 24 months → 0.75 penalty; ≥ 24 months → no penalty."""
    return 0.75 if months < 24 else 1.00


def _qoq_multiplier(qoq: float) -> float:
    """qoq is decimal fraction e.g. −0.028. QoQ bands from PDF."""
    if qoq > -0.10:  return 1.00   # > −10%   — no penalty
    if qoq > -0.20:  return 0.90   # −10% to −20%
    if qoq > -0.40:  return 0.80   # −20% to −40%
    return                 0.60   # < −40%


def _risk_band(combined: float) -> tuple[str, int]:
    """
    Risk Band = product of all 4 multipliers.
    Returns (band_name, tenure_multiplier).
    """
    # PDF v1.2 tenure bands:
    #   Low    → 48 months
    #   Medium → 36 months
    #   High   → 24 months
    if combined >= 0.75:  return "Low Risk",    48
    if combined >= 0.55:  return "Medium Risk", 36
    return                       "High Risk",   24


def _round_to(value: float, nearest: int) -> float:
    return round(value / nearest) * nearest
