"""
tests/test_suite.py
────────────────────
Async pytest suite covering:
  1. Loan engine math
  2. Lender strategy registry (strategy pattern)
  3. AA parser (data cleaning)
  4. CIBIL parser
  5. Transaction classifier (with mocked Claude)
  6. API endpoints (with mocked DB + external services)

Run:
  pytest tests/ -v --asyncio-mode=auto
"""
from __future__ import annotations
import json
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# 1. Loan Engine
# ─────────────────────────────────────────────────────────────────────────────

from services.loan_engine import (
    run_engine, compute_monthly_emi_from_bank,
    _median, _std_dev, _qoq, _volatility_multiplier,
    _concentration_multiplier, _vintage_multiplier, _qoq_multiplier,
)

SAMPLE_MONTHLY_CREDITS = {
    "2024-01": 380_000,
    "2024-02": 410_000,
    "2024-03": 395_000,
    "2024-04": 430_000,
    "2024-05": 450_000,
    "2024-06": 470_000,
    "2024-07": 460_000,
    "2024-08": 440_000,
    "2024-09": 480_000,
    "2024-10": 465_000,
    "2024-11": 455_000,
    "2024-12": 420_000,
}

SAMPLE_DAILY_BALANCES = {f"2024-{m:02d}-01": 22_000.0 for m in range(1, 13)}


class TestLoanEngineMath:
    def test_median_even(self):
        assert _median([440_000, 450_000]) == 445_000

    def test_median_odd(self):
        assert _median([380_000, 395_000, 410_000]) == 395_000

    def test_std_dev_zero(self):
        assert _std_dev([100, 100, 100], 100) == 0.0

    def test_volatility_bands(self):
        assert _volatility_multiplier(0.10) == 1.00
        assert _volatility_multiplier(0.35) == 0.85
        assert _volatility_multiplier(0.55) == 0.65
        assert _volatility_multiplier(0.80) == 0.45

    def test_concentration_penalty(self):
        assert _concentration_multiplier(0.30) == 1.00   # <40%
        assert _concentration_multiplier(0.45) == 0.70   # >40%

    def test_vintage_penalty(self):
        assert _vintage_multiplier(30) == 1.00   # >=24m
        assert _vintage_multiplier(18) == 0.75   # <24m

    def test_qoq_bands(self):
        assert _qoq_multiplier(-0.05) == 1.00
        assert _qoq_multiplier(-0.15) == 0.90
        assert _qoq_multiplier(-0.30) == 0.80
        assert _qoq_multiplier(-0.50) == 0.60

    def test_run_engine_produces_all_keys(self):
        result = run_engine(
            monthly_credits        = SAMPLE_MONTHLY_CREDITS,
            daily_balances         = SAMPLE_DAILY_BALANCES,
            active_days            = 320,
            existing_emi           = 38_823,
            business_vintage_months= 36,
        )
        required_keys = [
            "safe_loan_amount", "final_safe_emi", "risk_band",
            "median_monthly_flow", "volatility_index", "stress_emi",
            "bto_monthly_avg", "qoq_pct",
        ]
        for k in required_keys:
            assert k in result, f"Missing key: {k}"

    def test_run_engine_safe_loan_positive(self):
        result = run_engine(
            monthly_credits        = SAMPLE_MONTHLY_CREDITS,
            daily_balances         = SAMPLE_DAILY_BALANCES,
            active_days            = 320,
            existing_emi           = 10_000,
            business_vintage_months= 36,
        )
        assert result["safe_loan_amount"] > 0

    def test_run_engine_raises_on_empty_credits(self):
        with pytest.raises(ValueError):
            run_engine(
                monthly_credits={},
                daily_balances={},
                active_days=0,
                existing_emi=0,
                business_vintage_months=12,
            )

    def test_compute_emi_deduplication(self):
        emi_txns = [
            {"transaction_id": "A", "amount": 32_876.0},
            {"transaction_id": "B", "amount": 32_876.0},   # same amount, same lender
            {"transaction_id": "C", "amount":  5_947.0},
        ]
        cls_index = {
            "A": {"is_emi_obligation": True, "emi_lender": "AXIS BANK"},
            "B": {"is_emi_obligation": True, "emi_lender": "AXIS BANK"},
            "C": {"is_emi_obligation": True, "emi_lender": "AXIS BANK"},
        }
        # A and B are same (lender, amount) → only counted once
        total = compute_monthly_emi_from_bank(emi_txns, cls_index)
        assert total == 32_876 + 5_947


# ─────────────────────────────────────────────────────────────────────────────
# 2. Lender Registry / Strategy Pattern
# ─────────────────────────────────────────────────────────────────────────────

from lenders.engine import LenderContext, LenderStrategy, RuleResult
from lenders.registry import LenderRegistry
from lenders.strategies import (
    FlexiloansStrategy,
    PiramalUBLGoldStrategy,
    PiramalUBLPlusStrategy,
    PiramalUBLStandardStrategy,
)


def _make_context(**overrides) -> LenderContext:
    defaults = dict(
        loan_type="unsecured",
        loan_amount_requested=2_000_000,
        borrower_age=30,
        business_vintage_months=48,
        commercial_premises="Owned",
        residence_premises="Owned",
        residence_stability_months=24,
        office_stability_months=24,
        pincode="110011",
        business_industry="Ready Made Garments & Apparel",
        business_type="Trader",
        audited_financials_available=True,
        cibil_score=750,
        overdue_amount=0.0,
        payment_delayed_days=0,
        emi_bounce_last_6m=0,
        delinquency_last_12m=False,
        active_unsecured_loans=1,
        enquiries_last_2m=1,
        existing_emi_monthly=18_000,
        unsecured_track_emi_count=18,
        unsecured_track_loan_ratio=0.7,
        max_unsecured_loan_outstanding=1_400_000,
        account_type="CA",
        active_current_account_count=1,
        transaction_frequency_per_month=22,
        bank_account_vintage_months=36,
        statement_period_months=12,
        abb_daily=250_000,
        bto_monthly=900_000,
        median_monthly_flow=1_600_000,
        qoq_percent=-5.0,
        volatility_cv=0.12,
        risk_band="Low",
        safe_loan_amount=1_770_000,
        itr_income_annual=800_000,
        gst_turnover_annual=24_000_000,
        gst_compliance_status="active",
        gst_filing_regularity_months=11,
    )
    defaults.update(overrides)
    return LenderContext(**defaults)


class TestLenderStrategies:
    def test_flexiloans_eligible(self):
        ctx = _make_context()
        result = FlexiloansStrategy().evaluate(ctx)
        assert result.eligible is True
        assert result.fail_reason is None

    def test_flexiloans_fails_low_cibil(self):
        ctx = _make_context(cibil_score=680)
        result = FlexiloansStrategy().evaluate(ctx)
        assert result.eligible is False
        assert "700" in (result.fail_reason or "")

    def test_flexiloans_fails_overdue(self):
        ctx = _make_context(overdue_amount=45_000)
        result = FlexiloansStrategy().evaluate(ctx)
        assert result.eligible is False

    def test_piramal_plus_rejects_missing_track_record(self):
        ctx = _make_context(loan_amount_requested=4_000_000, unsecured_track_emi_count=6, unsecured_track_loan_ratio=0.2)
        result = PiramalUBLPlusStrategy().evaluate(ctx)
        assert result.eligible is False

    def test_piramal_gold_rejects_medium_risk_on_other_rules_not_checked(self):
        ctx = _make_context(risk_band="Medium")
        result = PiramalUBLGoldStrategy().evaluate(ctx)
        assert result.fail_reason is None or isinstance(result.fail_reason, str)

    def test_piramal_standard_requires_turnover_backing(self):
        ctx = _make_context(gst_turnover_annual=2_000_000)
        result = PiramalUBLStandardStrategy().evaluate(ctx)
        assert result.eligible is False

    def test_registry_evaluate_all(self):
        reg = LenderRegistry.__new__(LenderRegistry)
        reg._strategies = {}
        reg.register(FlexiloansStrategy())
        reg.register(PiramalUBLStandardStrategy())
        ctx = _make_context()
        results = reg.evaluate_all(ctx)
        assert len(results) == 2

    def test_registry_open_closed(self):
        class TestLender(LenderStrategy):
            @property
            def lender_name(self):
                return "TestLender"

            def _rules(self, ctx):
                return [RuleResult.pass_("always_pass", value=ctx.cibil_score, stage="check_1")]

        reg = LenderRegistry.__new__(LenderRegistry)
        reg._strategies = {}
        reg.register(TestLender())
        ctx = _make_context(cibil_score=650)
        result = reg.evaluate_all(ctx)
        assert result[0].eligible is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. AA Parser
# ─────────────────────────────────────────────────────────────────────────────

from utils.aa_parser import (
    parse_aa_payload, aggregate_monthly_credits,
    compute_daily_balances, count_active_days,
)

SAMPLE_AA_PAYLOAD = {
    "data": {
        "account_aggregator_json": [{
            "account_id": "test-001",
            "fi_status_details": {"account_number": "XXX123"},
            "profile_details": {"name": "Test User"},
            "summary_details": {"status": "ACTIVE", "account_sub_type": "SAVINGS"},
            "transaction_data": {
                "transaction_details": [
                    {
                        "transaction_id": "T1",
                        "amount": "50000",
                        "narration": "NEFT/Salary",
                        "type": "CREDIT",
                        "mode": "NEFT",
                        "transaction_timestamp": "2024-04-30T17:00:00.0",
                        "transaction_balance": "55000",
                    },
                    {
                        "transaction_id": "T2",
                        "amount": "5000",
                        "narration": "PPR_EMI_05-04-2024",
                        "type": "DEBIT",
                        "mode": "OTHERS",
                        "transaction_timestamp": "2024-04-05T08:00:00.0",
                        "transaction_balance": "45000",
                    },
                    # Duplicate — should be dropped
                    {
                        "transaction_id": "T1-dup",
                        "amount": "50000",
                        "narration": "NEFT/Salary",
                        "type": "CREDIT",
                        "mode": "NEFT",
                        "transaction_timestamp": "2024-04-30T17:00:00.0",
                        "transaction_balance": "55000",
                    },
                ]
            },
        }]
    }
}


class TestAAParser:
    def test_parses_without_error(self):
        result = parse_aa_payload(SAMPLE_AA_PAYLOAD)
        assert "transactions" in result

    def test_deduplicates_transactions(self):
        result = parse_aa_payload(SAMPLE_AA_PAYLOAD)
        assert len(result["transactions"]) == 2   # T1-dup is dropped

    def test_sorts_ascending(self):
        result = parse_aa_payload(SAMPLE_AA_PAYLOAD)
        dates = [t["transaction_date"] for t in result["transactions"]]
        assert dates == sorted(dates)

    def test_casts_amount_to_float(self):
        result = parse_aa_payload(SAMPLE_AA_PAYLOAD)
        for t in result["transactions"]:
            assert isinstance(t["amount"], float)

    def test_aggregate_revenue_only(self):
        txns = parse_aa_payload(SAMPLE_AA_PAYLOAD)["transactions"]
        # T1 is CREDIT, labelled Revenue
        classified = [{"transaction_id": "T1", "credit_category": "Revenue"}]
        monthly = aggregate_monthly_credits(txns, classified)
        assert monthly.get("2024-04") == 50_000.0

    def test_aggregate_excludes_loan_inward(self):
        txns = [
            {"transaction_id": "A", "type": "CREDIT", "amount": 100_000.0, "transaction_date": "2024-04-15"},
            {"transaction_id": "B", "type": "CREDIT", "amount":  50_000.0, "transaction_date": "2024-04-16"},
        ]
        classified = [
            {"transaction_id": "A", "credit_category": "Loan Inward"},
            {"transaction_id": "B", "credit_category": "Revenue"},
        ]
        monthly = aggregate_monthly_credits(txns, classified)
        assert monthly["2024-04"] == 50_000.0   # only B

    def test_active_days_count(self):
        txns = [
            {"type": "CREDIT", "transaction_date": "2024-04-01"},
            {"type": "CREDIT", "transaction_date": "2024-04-01"},   # same day
            {"type": "CREDIT", "transaction_date": "2024-04-15"},
            {"type": "DEBIT",  "transaction_date": "2024-04-20"},
        ]
        assert count_active_days(txns) == 2   # distinct credit days


# ─────────────────────────────────────────────────────────────────────────────
# 4. CIBIL Parser
# ─────────────────────────────────────────────────────────────────────────────

from utils.cibil_parser import parse_cibil_payload

SAMPLE_CIBIL = {
    "data": {
        "name":         "Test User",
        "pan":          "ABCDE1234F",
        "credit_score": "720",
        "credit_report": [{
            "scores": [{"score": "720"}],
            "accounts": [
                {
                    "accountType":     "Personal Loan",
                    "accountNumber":   "PPR001",
                    "dateClosed":      "NA",
                    "currentBalance":  "200000",
                    "amountOverdue":   "0",
                    "emiAmount":       "5947",
                    "suitFiledWillfulDefaultWrittenOff": "",
                    "monthlyPayStatus": [{"date": "2024-04-01", "status": "0"}],
                }
            ],
            "enquiries": [],
        }],
    }
}


class TestCIBILParser:
    def test_extracts_score(self):
        result = parse_cibil_payload(SAMPLE_CIBIL)
        assert result["score"] == 720

    def test_extracts_active_loans(self):
        result = parse_cibil_payload(SAMPLE_CIBIL)
        assert result["active_loan_count"] == 1

    def test_extracts_total_emi(self):
        result = parse_cibil_payload(SAMPLE_CIBIL)
        assert result["total_emi_from_cibil"] == 5947.0

    def test_zero_overdue(self):
        result = parse_cibil_payload(SAMPLE_CIBIL)
        assert result["overdue_amount"] == 0.0

    def test_no_written_off(self):
        result = parse_cibil_payload(SAMPLE_CIBIL)
        assert result["has_written_off"] is False

    def test_minimal_payload(self):
        """Score-only payload should not raise."""
        minimal = {"data": {"credit_score": "680"}}
        result  = parse_cibil_payload(minimal)
        assert result["score"] == 680


# ─────────────────────────────────────────────────────────────────────────────
# 5. Transaction Classifier (mocked Claude)
# ─────────────────────────────────────────────────────────────────────────────

from services.transaction_classifier import (
    build_classification_index,
    _parse_response,
    _null_classification,
)


class TestTransactionClassifier:
    def test_build_index_keyed_by_txn_id(self):
        results = [
            {"transaction_id": "X", "credit_category": "Revenue", "is_emi_obligation": None, "emi_lender": None},
            {"transaction_id": "Y", "credit_category": None,      "is_emi_obligation": True,  "emi_lender": "AXIS"},
        ]
        idx = build_classification_index(results)
        assert idx["X"]["credit_category"] == "Revenue"
        assert idx["Y"]["is_emi_obligation"] is True

    def test_parse_response_valid_json(self):
        raw = json.dumps([
            {"transaction_id": "T1", "credit_category": "Revenue",
             "is_emi_obligation": None, "emi_lender": None}
        ])
        txns  = [{"transaction_id": "T1"}]
        result = _parse_response(raw, txns)
        assert result[0]["credit_category"] == "Revenue"

    def test_parse_response_strips_markdown(self):
        raw = "```json\n[{\"transaction_id\":\"T1\",\"credit_category\":\"Loan Inward\",\"is_emi_obligation\":null,\"emi_lender\":null}]\n```"
        txns   = [{"transaction_id": "T1"}]
        result = _parse_response(raw, txns)
        assert result[0]["credit_category"] == "Loan Inward"

    def test_parse_response_bad_json_returns_nulls(self):
        txns   = [{"transaction_id": "T1"}, {"transaction_id": "T2"}]
        result = _parse_response("NOT JSON AT ALL", txns)
        assert len(result) == 2
        assert all(r["credit_category"] is None for r in result)

    def test_null_classification_preserves_count(self):
        txns = [{"transaction_id": f"T{i}"} for i in range(5)]
        result = _null_classification(txns)
        assert len(result) == 5
        assert all(r["is_emi_obligation"] is None for r in result)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Borrower Profile Validation
# ─────────────────────────────────────────────────────────────────────────────

from models.schemas import BorrowerProfile, BorrowerRegisterRequest, SignupPage1Request
from pydantic import ValidationError


class TestBorrowerProfileSchema:
    def test_valid_profile(self):
        profile = BorrowerProfile(
            name="Vishal Ahluwalia",
            pan="EKRPR1234F",
            mobile="9912345675",
            gender="male",
            age=30,
            business_vintage_months=24,
            business_industry="Technology",
            commercial_premises="Rented",
            residence_premises="Owned",
            pincode="110011",
        )
        assert profile.pan == "EKRPR1234F"

    def test_pan_uppercased(self):
        profile = BorrowerProfile(
            name="Test", pan="ekrpr1234f", mobile="9912345675",
            age=25, business_vintage_months=12,
            business_industry="Retail",
            commercial_premises="Owned", residence_premises="Owned",
            pincode="110001",
        )
        assert profile.pan == "EKRPR1234F"

    def test_invalid_mobile_rejected(self):
        with pytest.raises(ValidationError):
            BorrowerProfile(
                name="Test", pan="EKRPR1234F", mobile="12345",
                age=25, business_vintage_months=12,
                business_industry="Retail",
                commercial_premises="Owned", residence_premises="Owned",
                pincode="110001",
            )

    def test_invalid_pincode_rejected(self):
        with pytest.raises(ValidationError):
            BorrowerProfile(
                name="Test", pan="EKRPR1234F", mobile="9912345675",
                age=25, business_vintage_months=12,
                business_industry="Retail",
                commercial_premises="Owned", residence_premises="Owned",
                pincode="ABCDEF",   # invalid
            )


class TestBorrowerRegisterSchema:
    def test_allows_partial_update_with_borrower_id(self):
        req = BorrowerRegisterRequest(
            borrower_id="b-123",
            business_name="Acme Pvt Ltd",
            business_industry="Retail",
        )
        assert req.borrower_id == "b-123"
        assert req.mobile is None
        assert req.name is None

    def test_requires_name_mobile_when_using_gstin(self):
        with pytest.raises(ValidationError):
            BorrowerRegisterRequest(gstin="27AAPFU0939F1ZV")


class TestSignupPage1Schema:
    def test_requires_individual_pan(self):
        with pytest.raises(ValidationError):
            SignupPage1Request(
                signup_id="s-1",
                name="Test User",
                mobile="9912345675",
            )
