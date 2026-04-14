from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

VALID_RISK_BANDS: frozenset[str] = frozenset({"Low", "Medium", "High"})
VALID_PREMISES: frozenset[str] = frozenset({"Owned", "Rented", "Partially Owned"})
VALID_LOAN_TYPES: frozenset[str] = frozenset({"unsecured", "secured"})
VALID_ACCOUNT_TYPES: frozenset[str] = frozenset({"CA", "SB"})
VALID_GST_COMPLIANCE: frozenset[str] = frozenset({"active", "nil", "exempted"})


@dataclass(frozen=True)
class LenderContext:
    loan_type: str
    loan_amount_requested: float

    borrower_age: int
    business_vintage_months: int
    commercial_premises: str
    residence_premises: str
    residence_stability_months: Optional[int] = None
    office_stability_months: Optional[int] = None
    pincode: str = ""
    business_industry: str = ""
    business_type: str = ""
    audited_financials_available: Optional[bool] = None

    cibil_score: int = 0
    overdue_amount: float = 0.0
    payment_delayed_days: int = 0
    emi_bounce_last_6m: Optional[int] = None
    delinquency_last_12m: Optional[bool] = None
    active_unsecured_loans: Optional[int] = None
    enquiries_last_2m: Optional[int] = None
    existing_emi_monthly: float = 0.0
    unsecured_track_emi_count: Optional[int] = None
    unsecured_track_loan_ratio: Optional[float] = None
    max_unsecured_loan_outstanding: Optional[float] = None

    account_type: Optional[str] = None
    active_current_account_count: Optional[int] = None
    transaction_frequency_per_month: Optional[float] = None
    bank_account_vintage_months: Optional[int] = None
    statement_period_months: Optional[int] = None

    abb_daily: float = 0.0
    bto_monthly: float = 0.0
    median_monthly_flow: float = 0.0
    qoq_percent: float = 0.0
    volatility_cv: float = 0.0
    risk_band: str = "High"
    safe_loan_amount: float = 0.0

    itr_income_annual: Optional[float] = None
    gst_turnover_annual: Optional[float] = None
    gst_compliance_status: Optional[str] = None
    gst_filing_regularity_months: Optional[int] = None

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.loan_type not in VALID_LOAN_TYPES:
            errors.append(f"loan_type '{self.loan_type}' must be one of {VALID_LOAN_TYPES}")
        if self.loan_amount_requested <= 0:
            errors.append("loan_amount_requested must be > 0")
        if not (18 <= self.borrower_age <= 80):
            errors.append(f"borrower_age {self.borrower_age} out of range 18-80")
        if self.business_vintage_months < 0:
            errors.append("business_vintage_months cannot be negative")
        if self.commercial_premises not in VALID_PREMISES:
            errors.append(f"commercial_premises '{self.commercial_premises}' must be one of {VALID_PREMISES}")
        if self.residence_premises not in VALID_PREMISES:
            errors.append(f"residence_premises '{self.residence_premises}' must be one of {VALID_PREMISES}")
        if not (300 <= self.cibil_score <= 900):
            errors.append(f"cibil_score {self.cibil_score} out of range 300-900")
        if self.overdue_amount < 0:
            errors.append("overdue_amount cannot be negative")
        if self.payment_delayed_days < 0:
            errors.append("payment_delayed_days cannot be negative")
        if self.existing_emi_monthly < 0:
            errors.append("existing_emi_monthly cannot be negative")
        if self.abb_daily < 0 or self.bto_monthly < 0 or self.median_monthly_flow < 0:
            errors.append("banking metrics cannot be negative")
        if self.volatility_cv < 0:
            errors.append("volatility_cv cannot be negative")
        if self.risk_band not in VALID_RISK_BANDS:
            errors.append(f"risk_band '{self.risk_band}' must be one of {VALID_RISK_BANDS}")
        if self.account_type and self.account_type not in VALID_ACCOUNT_TYPES:
            errors.append(f"account_type '{self.account_type}' must be one of {VALID_ACCOUNT_TYPES}")
        if self.gst_compliance_status and self.gst_compliance_status not in VALID_GST_COMPLIANCE:
            errors.append(
                f"gst_compliance_status '{self.gst_compliance_status}' must be one of {VALID_GST_COMPLIANCE}"
            )
        if errors:
            raise ValueError(
                f"LenderContext validation failed ({len(errors)} error(s)):\n"
                + "\n".join(f"  - {err}" for err in errors)
            )

    @property
    def any_property_owned(self) -> bool:
        return self.commercial_premises != "Rented" or self.residence_premises != "Rented"

    @property
    def foir(self) -> Optional[float]:
        if not self.itr_income_annual or self.itr_income_annual <= 0:
            return None
        monthly_income = self.itr_income_annual / 12
        return self.existing_emi_monthly / monthly_income if monthly_income > 0 else None


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    passed: bool
    reason: Optional[str] = None
    value: Optional[Any] = None
    threshold: Optional[Any] = None
    stage: Optional[str] = None
    skipped: bool = False

    def __post_init__(self) -> None:
        if not self.passed and not self.reason:
            raise ValueError(f"RuleResult '{self.rule_name}' requires a reason when failed")
        if self.skipped and not self.passed:
            raise ValueError(f"RuleResult '{self.rule_name}' cannot be skipped and failed")

    @classmethod
    def pass_(
        cls,
        rule_name: str,
        *,
        reason: Optional[str] = None,
        value: Optional[Any] = None,
        threshold: Optional[Any] = None,
        stage: Optional[str] = None,
    ) -> "RuleResult":
        return cls(rule_name, True, reason, value, threshold, stage, False)

    @classmethod
    def fail_(
        cls,
        rule_name: str,
        reason: str,
        *,
        value: Optional[Any] = None,
        threshold: Optional[Any] = None,
        stage: Optional[str] = None,
    ) -> "RuleResult":
        return cls(rule_name, False, reason, value, threshold, stage, False)

    @classmethod
    def skip_(
        cls,
        rule_name: str,
        reason: str,
        *,
        value: Optional[Any] = None,
        threshold: Optional[Any] = None,
        stage: Optional[str] = None,
    ) -> "RuleResult":
        return cls(rule_name, True, reason, value, threshold, stage, True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "passed": self.passed,
            "reason": self.reason,
            "value": self.value,
            "threshold": self.threshold,
            "stage": self.stage,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class LenderDecisionResult:
    lender_name: str
    eligible: bool
    fail_reason: Optional[str]
    rule_details: tuple[RuleResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.rule_details, list):
            object.__setattr__(self, "rule_details", tuple(self.rule_details))

    @property
    def passed_rules(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rule_details if r.passed and not r.skipped)

    @property
    def failed_rules(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rule_details if not r.passed)

    @property
    def skipped_rules(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rule_details if r.skipped)

    @property
    def pass_count(self) -> int:
        return len(self.passed_rules)

    @property
    def fail_count(self) -> int:
        return len(self.failed_rules)

    @property
    def skip_count(self) -> int:
        return len(self.skipped_rules)

    @property
    def all_fail_reasons(self) -> list[str]:
        return [r.reason for r in self.failed_rules if r.reason]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lender_name": self.lender_name,
            "eligible": self.eligible,
            "fail_reason": self.fail_reason,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "skip_count": self.skip_count,
            "rule_details": [r.to_dict() for r in self.rule_details],
        }


@dataclass(frozen=True)
class LenderConfig:
    id: str
    name: str
    loan_type: str
    min_amount: int
    max_amount: int
    min_age: int
    max_age: int
    geography_mode: str = "pan_india"
    excluded_states: tuple[str, ...] = ()
    excluded_pincodes: tuple[str, ...] = ()
    vintage_rules: tuple[dict[str, Any], ...] = ()
    min_cibil: Optional[int] = None
    max_overdue_amount: Optional[float] = None
    max_payment_delay_days: Optional[int] = None
    max_emi_bounces_6m: Optional[int] = None
    delinquency_allowed: Optional[bool] = None
    max_enquiries_2m: Optional[int] = None
    max_active_unsecured_loans: Optional[int] = None
    min_unsecured_track_emi_count: Optional[int] = None
    min_unsecured_track_loan_ratio: Optional[float] = None
    min_abb_rules: tuple[dict[str, Any], ...] = ()
    min_bto_rules: tuple[dict[str, Any], ...] = ()
    max_qoq_decline: Optional[float] = None
    min_itr_income_rules: tuple[dict[str, Any], ...] = ()
    turnover_rules: tuple[dict[str, Any], ...] = ()
    foir_rules: tuple[dict[str, Any], ...] = ()
    min_monthly_credits_rules: tuple[dict[str, Any], ...] = ()
    account_type_rules: tuple[dict[str, Any], ...] = ()
    max_current_accounts: Optional[int] = None
    min_transaction_frequency_rules: tuple[dict[str, Any], ...] = ()
    min_account_vintage_months: Optional[int] = None
    statement_period_months: Optional[int] = None
    gst_compliance_allowed: Optional[tuple[str, ...]] = None
    audited_financials_rules: tuple[dict[str, Any], ...] = ()
    residence_stability_rules: Optional[dict[str, int]] = None
    office_stability_rules: Optional[dict[str, int]] = None


class RuleChecks:
    @staticmethod
    def pass_or_fail(
        *,
        stage: str,
        rule_name: str,
        passed: bool,
        value: Any,
        threshold: Any,
        fail_reason: str,
    ) -> RuleResult:
        if passed:
            return RuleResult.pass_(rule_name, value=value, threshold=threshold, stage=stage)
        return RuleResult.fail_(rule_name, fail_reason, value=value, threshold=threshold, stage=stage)

    @staticmethod
    def skip(stage: str, rule_name: str, reason: str, *, value: Any = None, threshold: Any = None) -> RuleResult:
        return RuleResult.skip_(rule_name, reason, value=value, threshold=threshold, stage=stage)


class LenderStrategy(RuleChecks, ABC):
    @property
    @abstractmethod
    def lender_name(self) -> str:
        ...

    @abstractmethod
    def _rules(self, ctx: LenderContext) -> list[RuleResult]:
        ...

    def evaluate(self, ctx: LenderContext) -> LenderDecisionResult:
        results = self._rules(ctx)
        failed = [r for r in results if not r.passed]
        return LenderDecisionResult(
            lender_name=self.lender_name,
            eligible=len(failed) == 0,
            fail_reason=failed[0].reason if failed else None,
            rule_details=tuple(results),
        )
