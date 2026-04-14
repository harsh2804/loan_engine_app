"""Config-driven lender strategies aligned to Capaxis Lender Matching Engine v1.1."""
from __future__ import annotations

from typing import Any, Optional

from lenders.engine import LenderConfig, LenderContext, LenderStrategy, RuleResult
from lenders.registry import registry

_FLEXILOANS_EXCLUDED_PIN_PREFIXES = (
    "180", "181", "182", "184", "190", "191", "192", "193", "194", "795", "796", "797", "798", "799"
)


def _normalize_business_type(value: str) -> str:
    v = (value or "").strip().lower()
    mapping = {
        "retailer": "retail",
        "retail": "retail",
        "wholesaler": "trader",
        "wholesaler & retailer": "trader",
        "trader": "trader",
        "manufacturer": "manufacturer",
        "mfr": "manufacturer",
        "service provider": "service_sep",
        "service": "service_sep",
    }
    return mapping.get(v, v)


def _pick_rule(rules: tuple[dict[str, Any], ...], amount: float) -> Optional[dict[str, Any]]:
    for rule in rules:
        if amount <= rule["max_loan_amount"]:
            return rule
    return None


def _rule_or_skip(
    stage: str,
    rule_name: str,
    cfg_value: Any,
    actual_value: Any,
    predicate,
    fail_reason: str,
) -> RuleResult:
    if cfg_value is None:
        return RuleResult.skip_(rule_name, "Skipped: lender config has no threshold", value=actual_value, threshold=None, stage=stage)
    if actual_value is None:
        return RuleResult.skip_(rule_name, "Skipped: upstream input is not available yet", value=None, threshold=cfg_value, stage=stage)
    passed = predicate(actual_value, cfg_value)
    if passed:
        return RuleResult.pass_(rule_name, value=actual_value, threshold=cfg_value, stage=stage)
    return RuleResult.fail_(rule_name, fail_reason, value=actual_value, threshold=cfg_value, stage=stage)


class ConfigurableLenderStrategy(LenderStrategy):
    def __init__(self, config: LenderConfig) -> None:
        self.config = config

    @property
    def lender_name(self) -> str:
        return self.config.name

    def _rules(self, ctx: LenderContext) -> list[RuleResult]:
        results: list[RuleResult] = []
        results.extend(self._check_universal_gates(ctx))
        if any(not r.passed for r in results):
            return results
        bureau_results = self._check_bureau_rules(ctx)
        results.extend(bureau_results)
        if any(not r.passed for r in bureau_results):
            return results
        results.extend(self._check_financial_rules(ctx))
        return results

    def _check_universal_gates(self, ctx: LenderContext) -> list[RuleResult]:
        c = self.config
        results = [
            RuleResult.pass_("loan_type", value=ctx.loan_type, threshold=c.loan_type, stage="check_1")
            if ctx.loan_type == c.loan_type
            else RuleResult.fail_("loan_type", f"Loan type '{ctx.loan_type}' not supported", value=ctx.loan_type, threshold=c.loan_type, stage="check_1"),
            RuleResult.pass_("loan_amount_range", value=ctx.loan_amount_requested, threshold=(c.min_amount, c.max_amount), stage="check_1")
            if c.min_amount <= ctx.loan_amount_requested <= c.max_amount
            else RuleResult.fail_("loan_amount_range", f"Requested loan amount Rs {ctx.loan_amount_requested:,.0f} outside range Rs {c.min_amount:,.0f}-Rs {c.max_amount:,.0f}", value=ctx.loan_amount_requested, threshold=(c.min_amount, c.max_amount), stage="check_1"),
            RuleResult.pass_("borrower_age", value=ctx.borrower_age, threshold=(c.min_age, c.max_age), stage="check_1")
            if c.min_age <= ctx.borrower_age <= c.max_age
            else RuleResult.fail_("borrower_age", f"Borrower age {ctx.borrower_age} outside range {c.min_age}-{c.max_age}", value=ctx.borrower_age, threshold=(c.min_age, c.max_age), stage="check_1"),
        ]
        results.append(self._check_geography(ctx))
        results.append(self._check_vintage(ctx))
        return results

    def _check_geography(self, ctx: LenderContext) -> RuleResult:
        c = self.config
        if c.geography_mode == "pan_india":
            return RuleResult.pass_("geography", value=ctx.pincode, threshold="pan_india", stage="check_1")
        blocked = any(ctx.pincode.startswith(prefix) for prefix in c.excluded_pincodes)
        if blocked:
            return RuleResult.fail_("geography", f"Pincode {ctx.pincode} is outside this lender's service geography", value=ctx.pincode, threshold=c.excluded_pincodes, stage="check_1")
        return RuleResult.pass_("geography", value=ctx.pincode, threshold=c.excluded_pincodes, stage="check_1")

    def _check_vintage(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.vintage_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("business_vintage", "Skipped: lender config has no vintage rule", value=ctx.business_vintage_months, stage="check_1")
        min_months = rule["min_months_owned"] if ctx.any_property_owned else rule["min_months_rented"]
        if ctx.business_vintage_months >= min_months:
            return RuleResult.pass_("business_vintage", value=ctx.business_vintage_months, threshold=min_months, stage="check_1")
        return RuleResult.fail_("business_vintage", f"Business vintage {ctx.business_vintage_months} months below required {min_months} months", value=ctx.business_vintage_months, threshold=min_months, stage="check_1")

    def _check_bureau_rules(self, ctx: LenderContext) -> list[RuleResult]:
        c = self.config
        return [
            _rule_or_skip("check_2", "min_cibil", c.min_cibil, ctx.cibil_score, lambda actual, threshold: actual >= threshold, f"CIBIL {ctx.cibil_score} below minimum {c.min_cibil}"),
            _rule_or_skip("check_2", "max_overdue_amount", c.max_overdue_amount, ctx.overdue_amount, lambda actual, threshold: actual <= threshold, f"Overdue amount Rs {ctx.overdue_amount:,.0f} exceeds allowed Rs {c.max_overdue_amount:,.0f}" if c.max_overdue_amount is not None else "Overdue amount exceeds allowed threshold"),
            _rule_or_skip("check_2", "max_payment_delay_days", c.max_payment_delay_days, ctx.payment_delayed_days, lambda actual, threshold: actual <= threshold, f"Payment delayed by {ctx.payment_delayed_days} days; maximum allowed is {c.max_payment_delay_days}" if c.max_payment_delay_days is not None else "Payment delay exceeds allowed threshold"),
            _rule_or_skip("check_2", "max_emi_bounces_6m", c.max_emi_bounces_6m, ctx.emi_bounce_last_6m, lambda actual, threshold: actual <= threshold, f"EMI bounce count {ctx.emi_bounce_last_6m} exceeds allowed maximum {c.max_emi_bounces_6m}" if c.max_emi_bounces_6m is not None else "EMI bounce count exceeds allowed threshold"),
            _rule_or_skip("check_2", "delinquency_last_12m", c.delinquency_allowed, ctx.delinquency_last_12m, lambda actual, threshold: actual is False or threshold is True, "Delinquency present in the last 12 months"),
            _rule_or_skip("check_2", "max_enquiries_2m", c.max_enquiries_2m, ctx.enquiries_last_2m, lambda actual, threshold: actual <= threshold, f"Enquiries in last 2 months ({ctx.enquiries_last_2m}) exceed allowed maximum {c.max_enquiries_2m}" if c.max_enquiries_2m is not None else "Recent enquiries exceed allowed threshold"),
            _rule_or_skip("check_2", "max_active_unsecured_loans", c.max_active_unsecured_loans, ctx.active_unsecured_loans, lambda actual, threshold: actual <= threshold, f"Active unsecured loans ({ctx.active_unsecured_loans}) exceed allowed maximum {c.max_active_unsecured_loans}" if c.max_active_unsecured_loans is not None else "Active unsecured loans exceed allowed threshold"),
            _rule_or_skip("check_2", "min_unsecured_track_emi_count", c.min_unsecured_track_emi_count, ctx.unsecured_track_emi_count, lambda actual, threshold: actual >= threshold, f"Clean unsecured EMI track record {ctx.unsecured_track_emi_count} below required {c.min_unsecured_track_emi_count}" if c.min_unsecured_track_emi_count is not None else "Insufficient unsecured track record"),
            _rule_or_skip("check_2", "min_unsecured_track_loan_ratio", c.min_unsecured_track_loan_ratio, ctx.unsecured_track_loan_ratio, lambda actual, threshold: actual >= threshold, f"Maximum unsecured loan track record ratio {ctx.unsecured_track_loan_ratio:.2f} below required {c.min_unsecured_track_loan_ratio:.2f}" if c.min_unsecured_track_loan_ratio is not None and ctx.unsecured_track_loan_ratio is not None else "Insufficient unsecured track record ratio"),
        ]

    def _check_financial_rules(self, ctx: LenderContext) -> list[RuleResult]:
        results: list[RuleResult] = []
        loan_amount = ctx.loan_amount_requested
        min_abb_rule = _pick_rule(self.config.min_abb_rules, loan_amount)
        results.append(
            _rule_or_skip("check_3", "min_abb", min_abb_rule["min_abb"] if min_abb_rule else None, ctx.abb_daily, lambda actual, threshold: actual >= threshold, f"ABB Rs {ctx.abb_daily:,.0f} below required Rs {min_abb_rule['min_abb']:,.0f}" if min_abb_rule else "ABB below threshold")
        )
        min_bto_rule = _pick_rule(self.config.min_bto_rules, loan_amount)
        results.append(self._check_bto_rule(ctx, min_bto_rule))
        results.append(
            _rule_or_skip("check_3", "max_qoq_decline", self.config.max_qoq_decline, ctx.qoq_percent, lambda actual, threshold: actual >= threshold, f"QoQ decline {ctx.qoq_percent:.2f}% is worse than allowed floor {self.config.max_qoq_decline:.2f}%" if self.config.max_qoq_decline is not None else "QoQ decline exceeds threshold")
        )
        min_itr_rule = _pick_rule(self.config.min_itr_income_rules, loan_amount)
        results.append(
            _rule_or_skip("check_3", "min_itr_income", min_itr_rule["min_income"] if min_itr_rule else None, ctx.itr_income_annual, lambda actual, threshold: actual >= threshold, f"ITR income Rs {ctx.itr_income_annual:,.0f} below required Rs {min_itr_rule['min_income']:,.0f}" if min_itr_rule and ctx.itr_income_annual is not None else "ITR income below threshold")
        )
        results.append(self._check_turnover(ctx))
        results.append(self._check_foir(ctx))
        results.append(self._check_min_monthly_credits(ctx))
        results.append(self._check_account_type(ctx))
        results.append(
            _rule_or_skip("check_3", "max_current_accounts", self.config.max_current_accounts, ctx.active_current_account_count, lambda actual, threshold: actual <= threshold, f"Current account count {ctx.active_current_account_count} exceeds allowed maximum {self.config.max_current_accounts}" if self.config.max_current_accounts is not None else "Current account count exceeds threshold")
        )
        results.append(self._check_transaction_frequency(ctx))
        results.append(
            _rule_or_skip("check_3", "min_account_vintage_months", self.config.min_account_vintage_months, ctx.bank_account_vintage_months, lambda actual, threshold: actual > threshold, f"Bank account vintage {ctx.bank_account_vintage_months} months is not above required {self.config.min_account_vintage_months}" if self.config.min_account_vintage_months is not None else "Bank account vintage below threshold")
        )
        results.append(
            _rule_or_skip("check_3", "statement_period_months", self.config.statement_period_months, ctx.statement_period_months, lambda actual, threshold: actual >= threshold, f"Statement period {ctx.statement_period_months} months shorter than required {self.config.statement_period_months} months" if self.config.statement_period_months is not None else "Statement period shorter than required")
        )
        results.append(self._check_gst(ctx))
        results.append(self._check_audited_financials(ctx))
        results.append(self._check_stability(ctx.residence_premises, ctx.residence_stability_months, self.config.residence_stability_rules, "residence_stability_months"))
        results.append(self._check_stability(ctx.commercial_premises, ctx.office_stability_months, self.config.office_stability_rules, "office_stability_months"))
        return results

    def _check_bto_rule(self, ctx: LenderContext, rule: Optional[dict[str, Any]]) -> RuleResult:
        if not rule:
            return RuleResult.skip_("min_bto", "Skipped: lender config has no BTO rule", value=ctx.bto_monthly, stage="check_3")
        absolute = rule.get("min_bto_absolute")
        pct = rule.get("min_bto_pct_of_turnover")
        if absolute is not None:
            return _rule_or_skip("check_3", "min_bto", absolute, ctx.bto_monthly, lambda actual, threshold: actual >= threshold, f"Monthly BTO Rs {ctx.bto_monthly:,.0f} below required Rs {absolute:,.0f}")
        if pct is not None:
            if ctx.gst_turnover_annual is None:
                return RuleResult.skip_("min_bto", "Skipped: GST turnover is not available for percentage-based BTO rule", value=ctx.bto_monthly, threshold=pct, stage="check_3")
            threshold = (ctx.gst_turnover_annual / 12) * pct
            return _rule_or_skip("check_3", "min_bto_pct_turnover", threshold, ctx.bto_monthly, lambda actual, bound: actual >= bound, f"Monthly BTO Rs {ctx.bto_monthly:,.0f} below required {pct:.0%} of monthly GST turnover")
        return RuleResult.skip_("min_bto", "Skipped: lender config BTO rule is incomplete", value=ctx.bto_monthly, stage="check_3")

    def _check_turnover(self, ctx: LenderContext) -> RuleResult:
        biz_type = _normalize_business_type(ctx.business_type)
        applicable = [
            rule for rule in self.config.turnover_rules
            if ctx.loan_amount_requested <= rule["max_loan_amount"] and rule["business_type"] == biz_type
        ]
        if not applicable:
            return RuleResult.skip_("turnover", "Skipped: lender has no turnover rule for this business type and loan slab", value=ctx.gst_turnover_annual, stage="check_3")
        rule = applicable[0]
        return _rule_or_skip("check_3", "turnover", rule["min_turnover"], ctx.gst_turnover_annual, lambda actual, threshold: actual >= threshold, f"Annual turnover Rs {ctx.gst_turnover_annual:,.0f} below required Rs {rule['min_turnover']:,.0f}" if ctx.gst_turnover_annual is not None else "Annual turnover below threshold")

    def _check_foir(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.foir_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("foir", "Skipped: lender config has no FOIR rule", value=ctx.foir, stage="check_3")
        return _rule_or_skip("check_3", "foir", rule["max_foir"], ctx.foir, lambda actual, threshold: actual <= threshold, f"FOIR {ctx.foir:.2%} exceeds allowed maximum {rule['max_foir']:.2%}" if ctx.foir is not None else "FOIR exceeds allowed threshold")

    def _check_min_monthly_credits(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.min_monthly_credits_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("min_monthly_credits", "Skipped: lender config has no monthly credits rule", value=ctx.median_monthly_flow, stage="check_3")
        return _rule_or_skip("check_3", "min_monthly_credits", rule["min_credits"], ctx.median_monthly_flow, lambda actual, threshold: actual >= threshold, f"Median monthly flow Rs {ctx.median_monthly_flow:,.0f} below required Rs {rule['min_credits']:,.0f}")

    def _check_account_type(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.account_type_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("account_type", "Skipped: lender config has no account type rule", value=ctx.account_type, stage="check_3")
        return _rule_or_skip("check_3", "account_type", tuple(rule["allowed_types"]), ctx.account_type, lambda actual, threshold: actual in threshold, f"Account type '{ctx.account_type}' not allowed; expected one of {rule['allowed_types']}")

    def _check_transaction_frequency(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.min_transaction_frequency_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("transaction_frequency_per_month", "Skipped: lender config has no transaction frequency rule", value=ctx.transaction_frequency_per_month, stage="check_3")
        return _rule_or_skip("check_3", "transaction_frequency_per_month", rule["min_txn_per_month"], ctx.transaction_frequency_per_month, lambda actual, threshold: actual >= threshold, f"Transaction frequency {ctx.transaction_frequency_per_month:.1f}/month below required {rule['min_txn_per_month']}/month" if ctx.transaction_frequency_per_month is not None else "Transaction frequency below threshold")

    def _check_gst(self, ctx: LenderContext) -> RuleResult:
        allowed = self.config.gst_compliance_allowed
        if allowed is None:
            return RuleResult.skip_("gst_compliance_status", "Skipped: lender config has no GST compliance rule", value=ctx.gst_compliance_status, stage="check_3")
        return _rule_or_skip("check_3", "gst_compliance_status", allowed, ctx.gst_compliance_status, lambda actual, threshold: actual in threshold, f"GST compliance status '{ctx.gst_compliance_status}' not allowed; expected one of {list(allowed)}")

    def _check_audited_financials(self, ctx: LenderContext) -> RuleResult:
        rule = _pick_rule(self.config.audited_financials_rules, ctx.loan_amount_requested)
        if not rule:
            return RuleResult.skip_("audited_financials", "Skipped: lender config has no audited financials rule", value=ctx.audited_financials_available, stage="check_3")
        if not rule["required"]:
            return RuleResult.pass_("audited_financials", reason="Not mandatory for this lender slab", value=ctx.audited_financials_available, threshold=False, stage="check_3")
        return _rule_or_skip("check_3", "audited_financials", True, ctx.audited_financials_available, lambda actual, threshold: actual is threshold, "Audited financials are mandatory for this lender slab")

    def _check_stability(self, premises: str, months: Optional[int], rules: Optional[dict[str, int]], rule_name: str) -> RuleResult:
        if not rules:
            return RuleResult.skip_(rule_name, "Skipped: lender config has no stability rule", value=months, stage="check_3")
        threshold = rules.get("if_owned_months", 0) if premises != "Rented" else rules.get("if_rented_months", 0)
        if threshold == 0:
            return RuleResult.pass_(rule_name, reason="No minimum required for this premises type", value=months, threshold=threshold, stage="check_3")
        return _rule_or_skip("check_3", rule_name, threshold, months, lambda actual, bound: actual >= bound, f"{rule_name} {months} months below required {threshold} months" if months is not None else f"{rule_name} below required {threshold} months")


FLEXILOANS_CONFIG = LenderConfig(
    id="flexiloans",
    name="Flexiloans",
    loan_type="unsecured",
    min_amount=1,
    max_amount=3_000_000,
    min_age=21,
    max_age=65,
    geography_mode="exclusion_list",
    excluded_pincodes=_FLEXILOANS_EXCLUDED_PIN_PREFIXES,
    vintage_rules=(
        {"max_loan_amount": 3_000_000, "min_months_owned": 12, "min_months_rented": 24},
    ),
    min_cibil=700,
    max_overdue_amount=40_000,
    max_payment_delay_days=0,
    max_emi_bounces_6m=1,
    min_abb_rules=({"max_loan_amount": 3_000_000, "min_abb": 15_000},),
    min_bto_rules=({"max_loan_amount": 3_000_000, "min_bto_absolute": 200_000},),
    max_qoq_decline=-35.0,
    account_type_rules=({"max_loan_amount": 3_000_000, "allowed_types": ["CA"]},),
    statement_period_months=12,
    gst_compliance_allowed=("active",),
)

PIRAMAL_STANDARD_CONFIG = LenderConfig(
    id="piramal_ubl_standard",
    name="Piramal UBL Standard",
    loan_type="unsecured",
    min_amount=500_000,
    max_amount=3_000_000,
    min_age=23,
    max_age=65,
    vintage_rules=(
        {"max_loan_amount": 1_200_000, "min_months_owned": 24, "min_months_rented": 36},
        {"max_loan_amount": 3_000_000, "min_months_owned": 36, "min_months_rented": 36},
    ),
    min_cibil=700,
    max_overdue_amount=0,
    max_payment_delay_days=0,
    max_emi_bounces_6m=0,
    delinquency_allowed=False,
    min_abb_rules=({"max_loan_amount": 3_000_000, "min_abb": 30_000},),
    min_bto_rules=(
        {"max_loan_amount": 1_500_000, "min_bto_pct_of_turnover": 0.5},
        {"max_loan_amount": 3_000_000, "min_bto_pct_of_turnover": 0.5},
    ),
    min_itr_income_rules=({"max_loan_amount": 3_000_000, "min_income": 400_000},),
    turnover_rules=(
        {"max_loan_amount": 2_000_000, "business_type": "trader", "min_turnover": 10_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "trader", "min_turnover": 20_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "manufacturer", "min_turnover": 10_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "manufacturer", "min_turnover": 20_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "service_sep", "min_turnover": 2_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "service_sep", "min_turnover": 3_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "service_senp", "min_turnover": 3_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "service_senp", "min_turnover": 5_000_000},
    ),
    foir_rules=(
        {"max_loan_amount": 1_500_000, "max_foir": 1.0},
        {"max_loan_amount": 3_000_000, "max_foir": 0.35},
    ),
    account_type_rules=(
        {"max_loan_amount": 1_200_000, "allowed_types": ["CA", "SB"]},
        {"max_loan_amount": 3_000_000, "allowed_types": ["CA"]},
    ),
    max_current_accounts=2,
    min_transaction_frequency_rules=(
        {"max_loan_amount": 1_200_000, "min_txn_per_month": 4},
        {"max_loan_amount": 3_000_000, "min_txn_per_month": 8},
    ),
    min_account_vintage_months=12,
    statement_period_months=12,
    gst_compliance_allowed=("active", "nil", "exempted"),
    audited_financials_rules=({"max_loan_amount": 3_000_000, "required": False},),
    residence_stability_rules={"if_rented_months": 12, "if_owned_months": 0},
    office_stability_rules={"if_rented_months": 12, "if_owned_months": 6},
)

PIRAMAL_PLUS_CONFIG = LenderConfig(
    id="piramal_ubl_plus",
    name="Piramal UBL+",
    loan_type="unsecured",
    min_amount=3_000_000,
    max_amount=5_000_000,
    min_age=23,
    max_age=65,
    vintage_rules=({"max_loan_amount": 5_000_000, "min_months_owned": 48, "min_months_rented": 48},),
    min_cibil=700,
    max_overdue_amount=0,
    max_payment_delay_days=0,
    max_emi_bounces_6m=0,
    delinquency_allowed=False,
    max_enquiries_2m=6,
    max_active_unsecured_loans=6,
    min_unsecured_track_emi_count=12,
    min_unsecured_track_loan_ratio=0.5,
    min_abb_rules=({"max_loan_amount": 5_000_000, "min_abb": 200_000},),
    min_bto_rules=({"max_loan_amount": 5_000_000, "min_bto_pct_of_turnover": 0.5},),
    min_itr_income_rules=({"max_loan_amount": 5_000_000, "min_income": 500_000},),
    turnover_rules=(
        {"max_loan_amount": 5_000_000, "business_type": "trader", "min_turnover": 50_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "manufacturer", "min_turnover": 50_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "service_sep", "min_turnover": 20_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "service_senp", "min_turnover": 20_000_000},
    ),
    foir_rules=({"max_loan_amount": 5_000_000, "max_foir": 0.30},),
    min_monthly_credits_rules=({"max_loan_amount": 5_000_000, "min_credits": 1_500_000},),
    account_type_rules=({"max_loan_amount": 5_000_000, "allowed_types": ["CA"]},),
    max_current_accounts=2,
    min_transaction_frequency_rules=({"max_loan_amount": 5_000_000, "min_txn_per_month": 8},),
    min_account_vintage_months=12,
    statement_period_months=12,
    gst_compliance_allowed=("active",),
    audited_financials_rules=({"max_loan_amount": 5_000_000, "required": True},),
    residence_stability_rules={"if_rented_months": 12, "if_owned_months": 0},
    office_stability_rules={"if_rented_months": 12, "if_owned_months": 6},
)

PIRAMAL_GOLD_CONFIG = LenderConfig(
    id="piramal_ubl_gold",
    name="Piramal UBL Gold",
    loan_type="unsecured",
    min_amount=500_000,
    max_amount=5_000_000,
    min_age=23,
    max_age=65,
    vintage_rules=(
        {"max_loan_amount": 1_200_000, "min_months_owned": 24, "min_months_rented": 36},
        {"max_loan_amount": 3_000_000, "min_months_owned": 36, "min_months_rented": 36},
        {"max_loan_amount": 5_000_000, "min_months_owned": 48, "min_months_rented": 48},
    ),
    min_cibil=700,
    max_overdue_amount=0,
    max_payment_delay_days=0,
    max_emi_bounces_6m=0,
    delinquency_allowed=False,
    max_enquiries_2m=6,
    max_active_unsecured_loans=6,
    min_unsecured_track_emi_count=12,
    min_unsecured_track_loan_ratio=0.5,
    min_abb_rules=(
        {"max_loan_amount": 3_000_000, "min_abb": 30_000},
        {"max_loan_amount": 5_000_000, "min_abb": 400_000},
    ),
    min_bto_rules=(
        {"max_loan_amount": 3_000_000, "min_bto_pct_of_turnover": 0.5},
        {"max_loan_amount": 5_000_000, "min_bto_pct_of_turnover": 0.5},
    ),
    min_itr_income_rules=(
        {"max_loan_amount": 3_000_000, "min_income": 400_000},
        {"max_loan_amount": 5_000_000, "min_income": 500_000},
    ),
    turnover_rules=(
        {"max_loan_amount": 2_000_000, "business_type": "trader", "min_turnover": 10_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "trader", "min_turnover": 20_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "trader", "min_turnover": 50_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "manufacturer", "min_turnover": 10_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "manufacturer", "min_turnover": 20_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "manufacturer", "min_turnover": 50_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "service_sep", "min_turnover": 2_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "service_sep", "min_turnover": 3_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "service_sep", "min_turnover": 20_000_000},
        {"max_loan_amount": 2_000_000, "business_type": "service_senp", "min_turnover": 3_000_000},
        {"max_loan_amount": 3_000_000, "business_type": "service_senp", "min_turnover": 5_000_000},
        {"max_loan_amount": 5_000_000, "business_type": "service_senp", "min_turnover": 20_000_000},
    ),
    foir_rules=(
        {"max_loan_amount": 1_500_000, "max_foir": 1.0},
        {"max_loan_amount": 3_000_000, "max_foir": 0.35},
        {"max_loan_amount": 5_000_000, "max_foir": 0.30},
    ),
    min_monthly_credits_rules=({"max_loan_amount": 5_000_000, "min_credits": 1_500_000},),
    account_type_rules=(
        {"max_loan_amount": 1_200_000, "allowed_types": ["CA", "SB"]},
        {"max_loan_amount": 5_000_000, "allowed_types": ["CA"]},
    ),
    max_current_accounts=2,
    min_transaction_frequency_rules=(
        {"max_loan_amount": 1_200_000, "min_txn_per_month": 4},
        {"max_loan_amount": 5_000_000, "min_txn_per_month": 8},
    ),
    min_account_vintage_months=12,
    statement_period_months=12,
    gst_compliance_allowed=("active", "nil", "exempted"),
    audited_financials_rules=(
        {"max_loan_amount": 1_500_000, "required": False},
        {"max_loan_amount": 5_000_000, "required": True},
    ),
    residence_stability_rules={"if_rented_months": 12, "if_owned_months": 0},
    office_stability_rules={"if_rented_months": 12, "if_owned_months": 6},
)


class FlexiloansStrategy(ConfigurableLenderStrategy):
    def __init__(self) -> None:
        super().__init__(FLEXILOANS_CONFIG)


class PiramalUBLStandardStrategy(ConfigurableLenderStrategy):
    def __init__(self) -> None:
        super().__init__(PIRAMAL_STANDARD_CONFIG)


class PiramalUBLPlusStrategy(ConfigurableLenderStrategy):
    def __init__(self) -> None:
        super().__init__(PIRAMAL_PLUS_CONFIG)


class PiramalUBLGoldStrategy(ConfigurableLenderStrategy):
    def __init__(self) -> None:
        super().__init__(PIRAMAL_GOLD_CONFIG)


registry.register(FlexiloansStrategy())
registry.register(PiramalUBLStandardStrategy())
registry.register(PiramalUBLPlusStrategy())
registry.register(PiramalUBLGoldStrategy())
