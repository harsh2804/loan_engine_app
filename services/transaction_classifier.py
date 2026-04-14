"""
services/transaction_classifier.py
────────────────────────────────────
Claude Call 1 — classify all bank transactions in one batched call.

CREDIT → Revenue | Loan Inward | Own Transfer | Cash Deposit
DEBIT  → is_emi_obligation: bool + emi_lender

Batching:
  - Sends up to BATCH_SIZE transactions per Claude call
  - Concurrent execution of all batches (asyncio.gather)
  - Graceful fallback on JSON parse errors

Audit:
  - Passes application_id and audit_callback for DB logging
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Optional

import httpx

from config.settings import get_settings

AuditCallback = Callable[..., Coroutine[Any, Any, None]]

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """You are a financial transaction classifier for an Indian lending platform.

You receive a JSON array of bank transactions. For each transaction return EXACTLY one JSON object.

━━ CREDIT transactions ━━
Set "credit_category" to ONE of:
  • Revenue        – salary, business income, client payment (NEFT/IMPS from companies, UPI from customers)
  • Loan Inward    – loan disbursement (keywords: FINANCE, NBFC, SAPTHAGIRI, HYPRCOM, BRANCHX, loan disbursement)
  • Own Transfer   – borrower transferring between own accounts (same name appears in narration, "SAIFUL AM/HDFC BANK" style self-transfers)
  • Cash Deposit   – ATM deposit, cash credit, CD

━━ DEBIT transactions ━━
Set "is_emi_obligation" to true/false:
  • true  – recurring fixed EMI pattern: PPR..._EMI_, NACH, standing instruction for loan, ECS
  • false – all other debits (UPI payments, groceries, rent, utilities, ATM withdrawal)
If true → set "emi_lender" to short lender name from narration (e.g. "AXIS BANK", "HDFC", "BAJAJ FIN")

━━ Output rules ━━
• Return ONLY a raw JSON array – no markdown, no explanation, no extra text
• credit_category is null for DEBIT transactions
• is_emi_obligation and emi_lender are null for CREDIT transactions
• Each object: {"transaction_id":"...","credit_category":null|"...","is_emi_obligation":null|bool,"emi_lender":null|"..."}
"""


async def classify_transactions(
    transactions: list[dict],
    *,
    borrower_name: str = "",
    application_id: Optional[str] = None,
    audit_callback: Optional[AuditCallback] = None,
) -> list[dict]:
    """
    Classify all transactions.  Returns a flat list of classification dicts.
    Never raises — falls back to null classifications on any error.
    """
    if not transactions:
        return []

    settings = get_settings()
    chunks   = _chunk(transactions, settings.claude_batch_size)

    async with httpx.AsyncClient(timeout=settings.claude_timeout_seconds) as client:
        tasks = [
            _classify_batch(
                client,
                chunk,
                borrower_name=borrower_name,
                application_id=application_id,
                audit_callback=audit_callback,
            )
            for chunk in chunks
        ]
        batched = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[dict] = []
    for i, outcome in enumerate(batched):
        if isinstance(outcome, Exception):
            # Graceful fallback for this batch
            results.extend(_null_classification(chunks[i]))
        else:
            results.extend(outcome)

    return results


async def _classify_batch(
    client: httpx.AsyncClient,
    transactions: list[dict],
    *,
    borrower_name: str,
    application_id: Optional[str],
    audit_callback: Optional[AuditCallback],
) -> list[dict]:
    settings = get_settings()

    compact = [
        {
            "transaction_id": t["transaction_id"],
            "amount":         t["amount"],
            "narration":      t["narration"],
            "type":           t["type"],
            "mode":           t["mode"],
        }
        for t in transactions
    ]

    user_msg = (
        f"Borrower: {borrower_name}\n\n"
        f"Transactions:\n{json.dumps(compact, ensure_ascii=False)}"
    )

    request_body = {
        "model":    settings.claude_model,
        "max_tokens": 4096,
        "system":   SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }

    t0 = time.perf_counter()
    response = await client.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key":       settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type":    "application/json",
        },
        json=request_body,
    )
    duration_ms = (time.perf_counter() - t0) * 1000
    response.raise_for_status()
    resp_json = response.json()

    # Fire audit log
    if audit_callback:
        try:
            await audit_callback(
                service="CLAUDE",
                endpoint=ANTHROPIC_API_URL,
                method="POST",
                request_body={"model": settings.claude_model, "transaction_count": len(transactions)},
                response_body={"usage": resp_json.get("usage")},
                status_code=response.status_code,
                duration_ms=duration_ms,
                success=True,
                application_id=application_id,
            )
        except Exception:
            pass

    raw_text = "".join(
        block.get("text", "")
        for block in resp_json.get("content", [])
        if block.get("type") == "text"
    ).strip()

    return _parse_response(raw_text, transactions)


def _parse_response(raw_text: str, transactions: list[dict]) -> list[dict]:
    """Parse Claude's JSON output; fall back to nulls on any error."""
    # Strip accidental markdown fences
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1].lstrip("json").strip() if len(parts) > 1 else raw_text

    try:
        parsed: list[dict] = json.loads(raw_text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    return _null_classification(transactions)


def _null_classification(transactions: list[dict]) -> list[dict]:
    return [
        {
            "transaction_id":    t["transaction_id"],
            "credit_category":   None,
            "is_emi_obligation": None,
            "emi_lender":        None,
        }
        for t in transactions
    ]


def build_classification_index(results: list[dict]) -> dict[str, dict]:
    """Build {transaction_id → classification} lookup dict."""
    return {r["transaction_id"]: r for r in results}


def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]
