"""
lenders package
───────────────
Exposes all public symbols so callers can import from either:
    from lenders.base import LenderStrategy, LenderContext, RuleResult
    from lenders import LenderStrategy, LenderContext, RuleResult   ← also works
"""
from lenders.engine import (
    LenderStrategy,
    LenderContext,
    RuleResult,
    LenderDecisionResult,
    LenderConfig,
    RuleChecks,
    VALID_ACCOUNT_TYPES,
    VALID_GST_COMPLIANCE,
    VALID_LOAN_TYPES,
    VALID_RISK_BANDS,
    VALID_PREMISES,
)
from lenders.registry import LenderRegistry, registry

__all__ = [
    "LenderStrategy",
    "LenderContext",
    "RuleResult",
    "LenderDecisionResult",
    "LenderConfig",
    "RuleChecks",
    "VALID_ACCOUNT_TYPES",
    "VALID_GST_COMPLIANCE",
    "VALID_LOAN_TYPES",
    "VALID_RISK_BANDS",
    "VALID_PREMISES",
    "LenderRegistry",
    "registry",
]
