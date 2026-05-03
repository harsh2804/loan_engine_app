"""
services/summarizer.py
──────────────────────
Claude Call — generate Capaxis Phase 1 borrower-facing insights.

Purpose:
  Transform engine numbers into plain-language insights the borrower
  can actually understand and act on.

Two outputs from one call:
  1. safe_borrowing_insights  — 5-6 bullets explaining the safe borrowing limit
  2. lender_insights          — 1-2 sentences on lender matching outcome

Claude does NO math here — it only narrates numbers the engine has already
computed.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

import httpx

from config.settings import get_settings

AuditCallback = Callable[..., Coroutine[Any, Any, None]]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


SYSTEM_PROMPT = """You are a financial advisor at Capaxis, an MSME lending platform.
Your job is to explain a borrower's safe borrowing limit and lender match results
in clear, respectful, plain language — as if speaking to a small business owner
who is not a finance expert.

You will receive computed financial metrics. Use them to write:

SECTION 1 — SAFE BORROWING LIMIT (exactly 5 bullet points):
  • Bullet 1: What their CIBIL score signals about creditworthiness
  • Bullet 2: How their existing EMI obligations affect borrowing capacity
  • Bullet 3: Income stability — reference the volatility index CV value directly
  • Bullet 4: What the 30% revenue stress test reveals about repayment safety
  • Bullet 5: The safe loan amount and what it means for their business

SECTION 2 — LENDER MATCH (exactly 1 bullet point):
  • Bullet 1: Which lenders are likely to approve and why (or why none match)

Formatting rules:
  • Start every bullet with exactly "• " (bullet + space)
  • Use ₹ symbol for all amounts, rounded to nearest ₹1,000
  • Never exceed 30 words per bullet
  • Do NOT mention Claude, AI, models, or algorithms
  • Do NOT use headers, markdown, bold, or numbered lists
  • Return ONLY the 6 bullet points — no preamble, no explanation"""


@dataclass
class ClaudeInsights:
    safe_borrowing_bullets: list[str]   # 5 bullets for Product 1
    lender_match_bullet:    str          # 1 bullet for Product 2


async def generate_decision_summary(
    *,
    borrower_name:      str,
    # CIBIL facts
    cibil_score:        int,
    overdue_amount:     float,
    effective_emi_monthly: float,   # Combined existing EMI (bureau + bank, deduped)
    median_inflow:      float,
    volatility_index:   float,
    volatility_interp:  str,
    qoq_pct:            float,
    stress_emi:         float,
    final_safe_emi:     float,
    safe_loan_amount:   float,
    risk_band:          str,
    # Lender matching
    eligible_lenders:   list[str],
    ineligible_lenders: list[str],
    # Audit
    application_id:     Optional[str]       = None,
    audit_callback:     Optional[AuditCallback] = None,
) -> ClaudeInsights:
    """
    Calls Claude once and returns structured insights.
    Never raises — returns a hardcoded fallback on any error.
    """
    settings = get_settings()

    facts = {
        "borrower_name":             borrower_name,
        "cibil_score":               cibil_score,
        "overdue_amount_inr":        round(overdue_amount),
        "existing_emi_monthly_inr":  round(effective_emi_monthly),
        "median_monthly_revenue_inr": round(median_inflow),
        "volatility_index_cv":       round(volatility_index, 3),
        "income_stability":          volatility_interp,
        "qoq_revenue_growth_pct":    round(qoq_pct, 1),
        "stress_test_safe_emi_inr":  round(stress_emi),
        "final_safe_emi_inr":        round(final_safe_emi),
        "safe_loan_amount_inr":      round(safe_loan_amount),
        "risk_band":                 risk_band,
        "likely_to_approve":         eligible_lenders,
        "unlikely_to_approve":       ineligible_lenders,
    }

    request_body = {
        "model":      settings.claude_model,
        "max_tokens": 600,
        "system":     SYSTEM_PROMPT,
        "messages":   [{
            "role":    "user",
            "content": (
                f"Generate Capaxis insights for this borrower:\n\n"
                f"{json.dumps(facts, indent=2, ensure_ascii=False)}"
            ),
        }],
    }

    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=settings.claude_timeout_seconds) as client:
            response = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key":         settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type":      "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()

        duration_ms = (time.perf_counter() - t0) * 1000
        resp_json   = response.json()

        if audit_callback:
            try:
                await audit_callback(
                    service="CLAUDE_SUMMARY",
                    endpoint=ANTHROPIC_API_URL,
                    method="POST",
                    request_body={"model": settings.claude_model, "task": "capaxis_insights"},
                    response_body={"usage": resp_json.get("usage")},
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                    success=True,
                    application_id=application_id,
                )
            except Exception:
                pass

        raw_text = "".join(
            b.get("text", "")
            for b in resp_json.get("content", [])
            if b.get("type") == "text"
        ).strip()

        bullets = _parse_bullets(raw_text)
        return ClaudeInsights(
            safe_borrowing_bullets = bullets[:5],
            lender_match_bullet    = bullets[5] if len(bullets) > 5 else bullets[-1],
        )

    except Exception:
        return _fallback_insights(
            cibil_score=cibil_score,
            safe_loan_amount=safe_loan_amount,
            risk_band=risk_band,
            eligible_lenders=eligible_lenders,
            volatility_index=volatility_index,
            volatility_interp=volatility_interp,
        )


def _parse_bullets(raw: str) -> list[str]:
    lines = [
        ln.strip().lstrip("•").strip()
        for ln in raw.splitlines()
        if ln.strip().startswith("•")
    ]
    return lines if lines else [l.strip() for l in raw.splitlines() if l.strip()]


def _fallback_insights(
    *,
    cibil_score:       int,
    safe_loan_amount:  float,
    risk_band:         str,
    eligible_lenders:  list[str],
    volatility_index:  float,
    volatility_interp: str,
) -> ClaudeInsights:
    lenders = ", ".join(eligible_lenders) if eligible_lenders else "no lenders at this time"
    return ClaudeInsights(
        safe_borrowing_bullets=[
            f"Your CIBIL score of {cibil_score} reflects your credit history with lenders.",
            "Your existing loan EMIs have been accounted for in the repayment capacity calculation.",
            f"Income stability index (CV={volatility_index:.3f}) shows {volatility_interp.lower()} revenue.",
            "A 30% revenue stress test was applied to ensure the loan stays affordable in a downturn.",
            f"Based on your cashflow, you can safely borrow up to ₹{safe_loan_amount:,.0f} — {risk_band}.",
        ],
        lender_match_bullet=(
            f"Based on your profile, {lenders} {'are' if len(eligible_lenders) > 1 else 'is'} "
            "likely to approve your application."
            if eligible_lenders
            else "No lenders matched your current profile — improving your CIBIL score may help."
        ),
    )
