"""
services package
────────────────
Re-exports lightweight service symbols only.
LoanOrchestrator is intentionally excluded here — it depends on lenders
and database packages, so importing it at package level creates a circular
import chain at startup. Import it directly where needed:

    from services.loan_orchestrator import LoanOrchestrator
"""
from services.loan_engine import run_engine, compute_monthly_emi_from_bank
from services.transaction_classifier import (
    classify_transactions,
    build_classification_index,
)
from services.summarizer import generate_decision_summary

__all__ = [
    "run_engine",
    "compute_monthly_emi_from_bank",
    "classify_transactions",
    "build_classification_index",
    "generate_decision_summary",
]