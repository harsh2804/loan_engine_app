"""
lenders/base.py
────────────────
Foundation types and abstractions for the lender Strategy pattern.

Responsibilities split across four distinct units:

  LenderContext        — immutable input snapshot (frozen dataclass + validation)
  RuleResult           — immutable single-rule verdict (frozen dataclass + factories)
  LenderDecisionResult — immutable aggregated verdict (frozen dataclass + properties)
  RuleChecks           — stateless check helpers (single responsibility: one class,
                         one job — evaluate a named rule against a scalar value)
  LenderStrategy       — pure abstract interface (just lender_name + _rules() + evaluate())

SOLID alignment:
  S — RuleChecks owns check logic; LenderStrategy owns evaluation orchestration only
  O — New checks: add a method to RuleChecks. New lenders: new file + register().
      Neither class changes when the other grows.
  L — Every LenderStrategy subclass is a drop-in replacement
  I — LenderStrategy has a minimal interface; subclasses call only the checks they need
  D — Orchestrator depends on LenderStrategy (abstract), never on concrete lenders

How to add a new lender (zero changes to existing code):
  1. Create lenders/your_lender.py
  2. class YourLender(LenderStrategy): ...
  3. registry.register(YourLender())
  4. Import in lenders/__init__.py
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "LenderContext",
    "RuleResult",
    "LenderDecisionResult",
    "RuleChecks",
    "LenderStrategy",
    "VALID_RISK_BANDS",
    "VALID_PREMISES",
]

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_RISK_BANDS: frozenset[str] = frozenset({"Low Risk", "Medium Risk", "High Risk"})
VALID_PREMISES:   frozenset[str] = frozenset({"Owned", "Rented", "Partially Owned"})


# =============================================================================
# LenderContext — immutable input snapshot
# =============================================================================

@dataclass(frozen=True)
class LenderContext:
    """
    Immutable read-only snapshot of all data needed to evaluate lender rules.

    frozen=True ensures:
      - No rule accidentally mutates the context mid-evaluation
      - Safe to pass to concurrent rule evaluations
      - Hashable (can be used as a dict key or cached)

    Grouped into four logical sections:
      CIBIL data · Bank statement data · Borrower profile · Engine outputs
    """

    # ── CIBIL ─────────────────────────────────────────────────────────────────
    cibil_score:           int    # 300–900
    overdue_amount:        float  # ₹ total overdue across all accounts
    max_days_overdue:      int    # worst bucket in days (0, 30, 60, …, 180)
    active_loan_count:     int    # open accounts with positive balance
    has_written_off:       bool   # any wilful default / write-off on record
    recent_enquiries_90d:  int    # hard enquiries in last 90 days

    # ── Bank statement ────────────────────────────────────────────────────────
    bto_monthly_avg:       float  # average monthly banking turnover (₹)
    median_monthly_flow:   float  # median monthly Revenue credits (₹)
    volatility_index:      float  # CV = σ / μ  (0 = perfectly stable)
    active_days_ratio:     float  # fraction of days with ≥1 credit (0.0–1.0)
    qoq_pct:               float  # quarter-over-quarter growth as decimal fraction

    # ── Borrower profile ──────────────────────────────────────────────────────
    business_vintage_months: int  # months the business has been operating
    business_industry:       str  # e.g. "Technology", "Retail"
    commercial_premises:     str  # "Owned" | "Rented"
    residence_premises:      str  # "Owned" | "Rented"
    borrower_age:            int  # years (18–80)
    pincode:                 str  # 6-digit Indian pincode

    # ── Engine outputs ────────────────────────────────────────────────────────
    detected_existing_emi:  float  # monthly EMI from bank-statement analysis (₹)
    safe_loan_amount:        float  # engine's final recommended loan amount (₹)
    risk_band:               str   # "Low Risk" | "Medium Risk" | "High Risk"

    # ── Validation ────────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        errors: list[str] = []

        if not (300 <= self.cibil_score <= 900):
            errors.append(f"cibil_score {self.cibil_score} out of range 300–900")
        if self.overdue_amount < 0:
            errors.append(f"overdue_amount cannot be negative: {self.overdue_amount}")
        if self.max_days_overdue < 0:
            errors.append(f"max_days_overdue cannot be negative: {self.max_days_overdue}")
        if self.active_loan_count < 0:
            errors.append(f"active_loan_count cannot be negative: {self.active_loan_count}")
        if self.recent_enquiries_90d < 0:
            errors.append(f"recent_enquiries_90d cannot be negative: {self.recent_enquiries_90d}")
        if self.bto_monthly_avg < 0:
            errors.append(f"bto_monthly_avg cannot be negative: {self.bto_monthly_avg}")
        if self.median_monthly_flow < 0:
            errors.append(f"median_monthly_flow cannot be negative: {self.median_monthly_flow}")
        if not (0.0 <= self.volatility_index):
            errors.append(f"volatility_index cannot be negative: {self.volatility_index}")
        if not (0.0 <= self.active_days_ratio <= 1.0):
            errors.append(f"active_days_ratio {self.active_days_ratio} out of range 0.0–1.0")
        if self.business_vintage_months < 0:
            errors.append(f"business_vintage_months cannot be negative: {self.business_vintage_months}")
        if self.commercial_premises not in VALID_PREMISES:
            errors.append(f"commercial_premises '{self.commercial_premises}' must be one of {VALID_PREMISES}")
        if self.residence_premises not in VALID_PREMISES:
            errors.append(f"residence_premises '{self.residence_premises}' must be one of {VALID_PREMISES}")
        if not (18 <= self.borrower_age <= 80):
            errors.append(f"borrower_age {self.borrower_age} out of range 18–80")
        if self.detected_existing_emi < 0:
            errors.append(f"detected_existing_emi cannot be negative: {self.detected_existing_emi}")
        if self.safe_loan_amount < 0:
            errors.append(f"safe_loan_amount cannot be negative: {self.safe_loan_amount}")
        if self.risk_band not in VALID_RISK_BANDS:
            errors.append(f"risk_band '{self.risk_band}' must be one of {VALID_RISK_BANDS}")

        if errors:
            raise ValueError(
                f"LenderContext validation failed ({len(errors)} error(s)):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def is_qoq_declining(self) -> bool:
        """True when quarter-over-quarter revenue is falling."""
        return self.qoq_pct < 0

    @property
    def has_overdue(self) -> bool:
        """True when any overdue balance exists."""
        return self.overdue_amount > 0

    @property
    def is_stable(self) -> bool:
        """True when CV ≤ 0.25 (very stable income)."""
        return self.volatility_index <= 0.25


# =============================================================================
# RuleResult — immutable single-rule verdict
# =============================================================================

@dataclass(frozen=True)
class RuleResult:
    """
    Immutable result of evaluating one rule against one value.

    frozen=True ensures results cannot be altered after creation,
    which matters for audit trails stored in the database.

    Use the class-method factories (pass_ / fail_) instead of
    constructing directly — they enforce the reason/value contract.
    """
    rule_name:  str
    passed:     bool
    reason:     Optional[str] = None   # None when passed; required when failed
    value:      Optional[Any] = None   # actual value that was tested
    threshold:  Optional[Any] = None   # threshold / expectation that was applied

    def __post_init__(self) -> None:
        if not self.passed and not self.reason:
            raise ValueError(
                f"RuleResult '{self.rule_name}': a failing result must include a reason"
            )

    # ── Factory class methods ─────────────────────────────────────────────────

    @classmethod
    def pass_(
        cls,
        rule_name: str,
        *,
        value:     Optional[Any] = None,
        threshold: Optional[Any] = None,
    ) -> "RuleResult":
        """Construct a passing result."""
        return cls(
            rule_name=rule_name,
            passed=True,
            reason=None,
            value=value,
            threshold=threshold,
        )

    @classmethod
    def fail_(
        cls,
        rule_name: str,
        reason:    str,
        *,
        value:     Optional[Any] = None,
        threshold: Optional[Any] = None,
    ) -> "RuleResult":
        """Construct a failing result (reason is mandatory)."""
        return cls(
            rule_name=rule_name,
            passed=False,
            reason=reason,
            value=value,
            threshold=threshold,
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule":      self.rule_name,
            "passed":    self.passed,
            "reason":    self.reason,
            "value":     self.value,
            "threshold": self.threshold,
        }


# =============================================================================
# LenderDecisionResult — immutable aggregated verdict
# =============================================================================

@dataclass(frozen=True)
class LenderDecisionResult:
    """
    Immutable aggregated eligibility decision for one lender.

    frozen=True makes this safe to cache, log, and pass between layers
    without risk of accidental mutation.
    """
    lender_name:  str
    eligible:     bool
    fail_reason:  Optional[str]
    rule_details: tuple[RuleResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Coerce list → tuple so the dataclass stays truly frozen/hashable
        if isinstance(self.rule_details, list):
            object.__setattr__(self, "rule_details", tuple(self.rule_details))

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def passed_rules(self) -> tuple[RuleResult, ...]:
        """All rules that passed."""
        return tuple(r for r in self.rule_details if r.passed)

    @property
    def failed_rules(self) -> tuple[RuleResult, ...]:
        """All rules that failed — ordered by position (first failure = primary reason)."""
        return tuple(r for r in self.rule_details if not r.passed)

    @property
    def pass_count(self) -> int:
        return len(self.passed_rules)

    @property
    def fail_count(self) -> int:
        return len(self.failed_rules)

    @property
    def total_rules(self) -> int:
        return len(self.rule_details)

    @property
    def all_fail_reasons(self) -> list[str]:
        """Every failure reason (not just the first), for detailed reporting."""
        return [r.reason for r in self.failed_rules if r.reason]

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "lender_name":  self.lender_name,
            "eligible":     self.eligible,
            "fail_reason":  self.fail_reason,
            "pass_count":   self.pass_count,
            "fail_count":   self.fail_count,
            "rule_details": [r.to_dict() for r in self.rule_details],
        }


# =============================================================================
# RuleChecks — stateless check helpers
# =============================================================================

class RuleChecks:
    """
    Stateless factory class for standard lender rule evaluations.

    Single Responsibility: translate a raw value + threshold into a
    named, immutable RuleResult.  This is the ONLY class that knows
    how to phrase failure messages.

    Interface Segregation: LenderStrategy subclasses inherit these via
    the mixin, but only call the checks their rules actually need.
    Using @staticmethod means no instance state is ever created or
    leaked between evaluations.

    Adding a new check type:
      1. Add a @staticmethod method here
      2. Call it from whichever LenderStrategy needs it
      Zero changes to existing strategies or to LenderStrategy itself.
    """

    @staticmethod
    def _check_cibil(score: int, minimum: int) -> RuleResult:
        passed = score >= minimum
        if passed:
            return RuleResult.pass_("min_cibil_score", value=score, threshold=minimum)
        return RuleResult.fail_(
            "min_cibil_score",
            f"CIBIL {score} below minimum {minimum}",
            value=score,
            threshold=minimum,
        )

    @staticmethod
    def _check_overdue(amount: float, max_allowed: float) -> RuleResult:
        passed = amount <= max_allowed
        if passed:
            return RuleResult.pass_("max_overdue_amount", value=amount, threshold=max_allowed)
        return RuleResult.fail_(
            "max_overdue_amount",
            f"Overdue ₹{amount:,.0f} exceeds ₹{max_allowed:,.0f}",
            value=amount,
            threshold=max_allowed,
        )

    @staticmethod
    def _check_bto(bto: float, minimum: float) -> RuleResult:
        passed = bto >= minimum
        if passed:
            return RuleResult.pass_("min_monthly_bto", value=bto, threshold=minimum)
        return RuleResult.fail_(
            "min_monthly_bto",
            f"Monthly BTO ₹{bto:,.0f} below ₹{minimum:,.0f}",
            value=bto,
            threshold=minimum,
        )

    @staticmethod
    def _check_vintage(months: int, minimum: int) -> RuleResult:
        passed = months >= minimum
        if passed:
            return RuleResult.pass_("min_vintage_months", value=months, threshold=minimum)
        return RuleResult.fail_(
            "min_vintage_months",
            f"Vintage {months}m below minimum {minimum}m",
            value=months,
            threshold=minimum,
        )

    @staticmethod
    def _check_industry(industry: str, blocked: list[str]) -> RuleResult:
        passed = industry not in blocked
        if passed:
            return RuleResult.pass_("industry_allowed", value=industry, threshold=blocked)
        return RuleResult.fail_(
            "industry_allowed",
            f"Industry '{industry}' not served by this lender",
            value=industry,
            threshold=blocked,
        )

    @staticmethod
    def _check_written_off(has_written_off: bool) -> RuleResult:
        if not has_written_off:
            return RuleResult.pass_("no_written_off", value=False, threshold=False)
        return RuleResult.fail_(
            "no_written_off",
            "Write-off or wilful default present on credit record",
            value=True,
            threshold=False,
        )

    @staticmethod
    def _check_enquiries(count: int, max_allowed: int) -> RuleResult:
        passed = count <= max_allowed
        if passed:
            return RuleResult.pass_("max_recent_enquiries", value=count, threshold=max_allowed)
        return RuleResult.fail_(
            "max_recent_enquiries",
            f"{count} hard enquiries in last 90 days (maximum {max_allowed})",
            value=count,
            threshold=max_allowed,
        )

    @staticmethod
    def _check_volatility(cv: float, max_allowed: float) -> RuleResult:
        passed = cv <= max_allowed
        if passed:
            return RuleResult.pass_("max_volatility_index", value=cv, threshold=max_allowed)
        return RuleResult.fail_(
            "max_volatility_index",
            f"Volatility index CV={cv:.3f} exceeds maximum {max_allowed}",
            value=cv,
            threshold=max_allowed,
        )

    @staticmethod
    def _check_risk_band(band: str, allowed: list[str]) -> RuleResult:
        passed = band in allowed
        if passed:
            return RuleResult.pass_("risk_band_allowed", value=band, threshold=allowed)
        return RuleResult.fail_(
            "risk_band_allowed",
            f"Risk band '{band}' not eligible (allowed: {allowed})",
            value=band,
            threshold=allowed,
        )

    @staticmethod
    def _check_active_days(ratio: float, minimum: float) -> RuleResult:
        passed = ratio >= minimum
        if passed:
            return RuleResult.pass_("min_active_days_ratio", value=ratio, threshold=minimum)
        return RuleResult.fail_(
            "min_active_days_ratio",
            f"Active days ratio {ratio:.2f} below minimum {minimum}",
            value=ratio,
            threshold=minimum,
        )

    @staticmethod
    def _check_max_days_overdue(days: int, max_allowed: int) -> RuleResult:
        passed = days <= max_allowed
        if passed:
            return RuleResult.pass_("max_days_overdue", value=days, threshold=max_allowed)
        return RuleResult.fail_(
            "max_days_overdue",
            f"Payment delayed by {days} days (maximum {max_allowed})",
            value=days,
            threshold=max_allowed,
        )

    @staticmethod
    def _check_borrower_age(age: int, minimum: int, maximum: int = 65) -> RuleResult:
        passed = minimum <= age <= maximum
        if passed:
            return RuleResult.pass_("borrower_age_range", value=age, threshold=(minimum, maximum))
        return RuleResult.fail_(
            "borrower_age_range",
            f"Borrower age {age} outside allowed range {minimum}–{maximum}",
            value=age,
            threshold=(minimum, maximum),
        )


# =============================================================================
# LenderStrategy — pure abstract interface + template method
# =============================================================================

class LenderStrategy(RuleChecks, ABC):
    """
    Abstract strategy — one concrete class per lender.

    Inherits RuleChecks so subclasses can call self._check_*() directly.
    The only two things a subclass must define are:
      1. lender_name  — a unique string identifier
      2. _rules()     — return list[RuleResult] using self._check_* helpers

    The evaluate() template method is final — do NOT override it.
    It guarantees consistent aggregation behaviour across all lenders.

    Example:
        class MyLender(LenderStrategy):
            @property
            def lender_name(self) -> str:
                return "My Lender"

            def _rules(self, ctx: LenderContext) -> list[RuleResult]:
                return [
                    self._check_cibil(ctx.cibil_score, minimum=700),
                    self._check_overdue(ctx.overdue_amount, max_allowed=0),
                    self._check_bto(ctx.bto_monthly_avg, minimum=50_000),
                ]
    """

    @property
    @abstractmethod
    def lender_name(self) -> str:
        """Unique lender identifier — used as the registry key."""
        ...

    @abstractmethod
    def _rules(self, ctx: LenderContext) -> list[RuleResult]:
        """
        Return the list of RuleResult objects for this lender.
        Order matters: the FIRST failing result becomes the primary fail_reason.
        Put hard-gate rules (written-off, overdue) before soft rules (BTO, vintage).
        """
        ...

    def evaluate(self, ctx: LenderContext) -> LenderDecisionResult:
        """
        Template method — orchestrates rule evaluation and result aggregation.

        This method is intentionally NOT abstract and should NOT be overridden.
        All lender-specific logic belongs in _rules().
        """
        results: list[RuleResult] = self._rules(ctx)
        failed  = [r for r in results if not r.passed]

        return LenderDecisionResult(
            lender_name  = self.lender_name,
            eligible     = len(failed) == 0,
            fail_reason  = failed[0].reason if failed else None,
            rule_details = tuple(results),
        )