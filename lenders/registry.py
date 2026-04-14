"""
lenders/registry.py
────────────────────
Central registry for all LenderStrategy implementations.

Open/Closed:
  - New lenders register themselves at import time
  - The LenderRegistry.evaluate_all() method never changes
  - The orchestrator only ever calls evaluate_all() — no lender names hardcoded

Thread-safety:
  - Registry is populated once at startup
  - evaluate_all() is read-only after that
"""
from __future__ import annotations
from typing import Optional

from lenders.engine import LenderStrategy, LenderContext, LenderDecisionResult


class LenderRegistry:
    """
    Singleton registry.  All LenderStrategy instances register here.

    Usage:
        from lenders.registry import registry
        registry.register(FlexiLoansStrategy())
    """

    _instance: Optional["LenderRegistry"] = None

    def __new__(cls) -> "LenderRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._strategies: dict[str, LenderStrategy] = {}
        return cls._instance

    def register(self, strategy: LenderStrategy) -> None:
        """Register a lender strategy.  Idempotent."""
        self._strategies[strategy.lender_name] = strategy

    def unregister(self, lender_name: str) -> None:
        """Remove a lender (useful in tests)."""
        self._strategies.pop(lender_name, None)

    def get(self, lender_name: str) -> Optional[LenderStrategy]:
        return self._strategies.get(lender_name)

    def list_lenders(self) -> list[str]:
        return list(self._strategies.keys())

    def evaluate_all(self, ctx: LenderContext) -> list[LenderDecisionResult]:
        """
        Evaluate every registered lender against the given context.
        Returns one LenderDecisionResult per lender, sorted by name.
        """
        results = [
            strategy.evaluate(ctx)
            for strategy in self._strategies.values()
        ]
        return sorted(results, key=lambda r: r.lender_name)

    def evaluate_one(self, lender_name: str, ctx: LenderContext) -> Optional[LenderDecisionResult]:
        strategy = self.get(lender_name)
        return strategy.evaluate(ctx) if strategy else None


# Module-level singleton
registry = LenderRegistry()
