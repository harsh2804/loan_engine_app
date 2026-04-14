"""
models/schemas.py
──────────────────
Pydantic v2 schemas aligned with the Mizan conversation flow.

Mizan Phase Map → API Step Map:
  Phase 01  Loan type selection      → Step 2 (ApplicationStartRequest.loan_type)
  Phase 02  Hard gates 1-9           → Step 1 (BorrowerRegisterRequest) + Step 2
  Phase 03  CIBIL consent/OTP/fetch  → Steps 3-4
  Phase 04  AA consent/init/fetch    → Steps 5-7
  Phase 05  Results                  → Step 8

Hard Stops (enforced in orchestrator before any API call):
  Hard Stop A — no current account in business name (Gate 5)
  Hard Stop B — CIBIL score < 650 (after Phase 03 fetch)

Saved-once semantics:
  business_nature, business_industry, business_product,
  commercial_premises, residence_premises,
  whatsapp_number, has_current_account
  → Only sent on first registration. On return visits the API
    uses stored values and skips re-collecting them.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator
import re


# =============================================================================
# Step 1 — Register Borrower
# =============================================================================

class BorrowerRegisterRequest(BaseModel):
    """
    Collects all identity + profile fields.
    For returning borrowers, only identity fields are required —
    saved-once fields are optional (omit them and the stored values are kept).
    """
    # Identity — always required
    borrower_id: Optional[str] = Field(
        None,
        description="Existing borrower_id whose GSTIN/PAN/CIN/DOI are already stored in the signup table",
    )
    name:   Optional[str] = Field(
        None,
        min_length=2,
        max_length=200,
        description="Full legal name as on PAN card",
    )
    gstin:  Optional[str] = Field(
        None,
        pattern=r"^[0-9]{2}[A-Za-z]{5}[0-9]{4}[A-Za-z][A-Za-z0-9]Z[A-Za-z0-9]$",
        description="Legacy identifier. Prefer borrower_id.",
    )
    mobile: Optional[str] = Field(None, description="10-digit mobile starting with 6-9")
    email:         Optional[str] = None
    gender:        Optional[str] = Field(None, pattern="^(male|female|other)$")
    age:           Optional[int] = Field(None, ge=18, le=80)
    # Collected in Mizan Phase 03 CIBIL input step — format YYYY-MM-DD
    date_of_birth: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Date of birth in YYYY-MM-DD format",
    )

    # Business profile — SAVED_ONCE (optional on update)
    business_name:           Optional[str] = Field(None, description="Registered business name")
    business_nature:         Optional[str] = Field(
        None,
        description="Retailer | Wholesaler | Wholesaler & Retailer | Manufacturer | Service Provider | Trader",
    )
    business_industry:       Optional[str] = Field(
        None,
        description="e.g. Ready Made Garments & Apparel, Food & Food Products",
    )
    business_product:        Optional[str] = Field(
        None,
        description="What the business primarily sells — e.g. Men's sportswear and activewear",
    )
    business_vintage_months: Optional[int] = Field(None, ge=0)
    commercial_premises:     Optional[str] = Field(
        None, pattern="^(Owned|Rented|Partially Owned)$",
    )
    residence_premises:      Optional[str] = Field(
        None, pattern="^(Owned|Rented|Partially Owned)$",
    )
    pincode:                 Optional[str] = Field(None, pattern=r"^\d{6}$")

    # Contact — SAVED_ONCE
    whatsapp_number:    Optional[str] = Field(None, description="WhatsApp number (if different from mobile)")

    # Hard Gate 5 — SAVED_ONCE
    has_current_account: Optional[bool] = Field(
        None,
        description="True = business has a current account in its name. False = Hard Stop A.",
    )

    @model_validator(mode="after")
    def _require_identity_key(self) -> "BorrowerRegisterRequest":
        if not self.borrower_id and not self.gstin:
            raise ValueError("Either borrower_id or gstin is required.")
        if not self.borrower_id:
            if not self.name:
                raise ValueError("name is required when borrower_id is not provided.")
            if not self.mobile:
                raise ValueError("mobile is required when borrower_id is not provided.")
        return self

    @field_validator("gstin")
    @classmethod
    def uppercase_gstin(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Invalid Indian mobile — must be 10 digits starting with 6-9")
        return v

    @field_validator("whatsapp_number")
    @classmethod
    def validate_whatsapp(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Invalid WhatsApp number")
        return v


class BorrowerRegisterResponse(BaseModel):
    borrower_id:             str
    gstin:                   str
    #pan:                     str
    individual_pan:          Optional[str] = None
    cin:                     Optional[str] = None
    date_of_incorporation:   Optional[str] = None
    name:                    str
    is_new:                  bool     # True = first registration
    profile_complete:        bool     # True = all SAVED_ONCE fields captured
    missing_fields:          list[str]  # fields still needed (empty if complete)
    hard_stop:               Optional["HardStopResponse"] = None  # set if has_current_account=False
    message:                 str
    next_step:               str


# =============================================================================
# Step 0 â€” GSTIN Verification (pre-signup)
# =============================================================================

class GstinVerifyRequest(BaseModel):
    gstin: str = Field(
        ...,
        pattern=r"^[0-9]{2}[A-Za-z]{5}[0-9]{4}[A-Za-z][A-Za-z0-9]Z[A-Za-z0-9]$",
        description="GSTIN to verify and store in the signup table",
    )

    @field_validator("gstin")
    @classmethod
    def uppercase_verify_gstin(cls, v: str) -> str:
        return v.upper()


class GstinVerifyResponse(BaseModel):
    signup_id: str
    gstin: str
    pan: str
    cin: Optional[str] = None
    date_of_incorporation: Optional[str] = None
    next_step: str


class SignupPage1Request(BaseModel):
    signup_id: str
    name: str = Field(..., min_length=2, max_length=200)
    mobile: str = Field(..., description="10-digit mobile starting with 6-9")
    gender: Optional[str] = Field(None, pattern="^(male|female|other)$")
    date_of_birth: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Date of birth in YYYY-MM-DD format",
    )
    individual_pan: str = Field(
        ...,
        pattern=r"^[A-Za-z]{5}[0-9]{4}[A-Za-z]$",
        description="Individual PAN to be used for CIBIL checks",
    )
    company_pan: Optional[str] = Field(
        None,
        description="Optional confirmation of company PAN (validated against signup table if provided)",
    )

    @field_validator("individual_pan")
    @classmethod
    def uppercase_individual_pan(cls, v: str) -> str:
        return v.upper()

    @field_validator("company_pan")
    @classmethod
    def uppercase_company_pan(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    @field_validator("mobile")
    @classmethod
    def validate_signup_mobile(cls, v: str) -> str:
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Invalid Indian mobile â€” must be 10 digits starting with 6-9")
        return v

# =============================================================================
# Hard Stops
# =============================================================================

class HardStopResponse(BaseModel):
    """
    Returned whenever a hard gate is triggered.
    The frontend renders this as a blocking screen per the Mizan spec.
    """
    code:        str   # "HARD_STOP_A" | "HARD_STOP_B"
    reason:      str   # human-readable reason
    guidance:    str   # what the borrower can do to fix it
    cta_label:   str   # call-to-action button label e.g. "Start fresh analysis"


# =============================================================================
# Step 2 — Start Application
# =============================================================================

class ApplicationStartRequest(BaseModel):
    borrower_pan:        str
    loan_type:           str = Field(
        ..., description="Unsecured Term Loan | Secured Term Loan",
    )
    target_loan_amount:  float = Field(..., gt=0, description="Requested loan amount in ₹")

    @field_validator("borrower_pan")
    @classmethod
    def uppercase(cls, v: str) -> str:
        return v.upper()

    @field_validator("loan_type")
    @classmethod
    def validate_loan_type(cls, v: str) -> str:
        allowed = {"Unsecured Term Loan", "Secured Term Loan"}
        if v not in allowed:
            raise ValueError(f"loan_type must be one of {allowed}")
        return v


class ApplicationStartResponse(BaseModel):
    application_id:      str
    borrower_id:         str
    borrower_name:       str
    loan_type:           str
    target_loan_amount:  float
    status:              str
    message:             str
    next_step:           str


# =============================================================================
# Step 3 & 5 — Consent
# =============================================================================

class ConsentRequest(BaseModel):
    """
    Explicit borrower consent before any credit bureau or bank data pull.
    IP address and user-agent stored for regulatory audit trail.
    """
    consent:    str = Field(..., pattern="^(Y|N)$",
                            description="Y = consent given, N = withheld")
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


class ConsentResponse(BaseModel):
    application_id: str
    consent_type:   str    # "CIBIL" | "AA"
    consent_given:  bool
    recorded_at:    str    # ISO 8601
    status:         str
    message:        str
    next_step:      str


# =============================================================================
# Step 4 — CIBIL Fetch (with OTP + Hard Stop B)
# =============================================================================

class CibilOtpRequest(BaseModel):
    """
    Mizan Phase 03: OTP sent to borrower's registered mobile.
    Frontend collects this and calls the CIBIL fetch endpoint.
    """
    otp: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$",
                     description="6-digit OTP sent to PAN-registered mobile")


class CibilScoreBreakdown(BaseModel):
    """
    Parsed CIBIL report — shown in the Mizan Phase 03 result card.

    Note on EMI:
      total_emi_from_cibil is displayed on the result card so the borrower can
      see what CIBIL reports.  It is NOT fed into the engine calculation.
      The engine uses only AA-detected EMIs (is_emi_obligation=True DEBITs).
    """
    score:                 int
    score_interpretation:  str   # Excellent / Good / Fair / Poor
    overdue_amount:        float  # ₹
    max_days_overdue:      int    # DPD in days
    has_written_off:       bool
    active_loan_count:     int
    total_emi_from_cibil:  float  # ₹/month — display only, not used in engine
    recent_enquiries_90d:  int
    emi_bounce_last_6m:    Optional[int] = None
    enquiry_count_6m:      Optional[int] = None
    max_unsecured_loan_outstanding: Optional[float] = None


class CibilResultMessage(BaseModel):
    """
    Mizan-aligned result message after CIBIL fetch.
    Three possible variants from the conversation script:
      clean   — score > 650, no overdue, no delays
      issues  — score > 650, but overdue or delays present
    Hard Stop B is returned as a HardStopResponse (not this schema).
    """
    variant:  str    # "clean" | "issues"
    headline: str    # e.g. "Your credit profile is clean."
    detail:   str    # the full Mizan message


class CibilFetchResponse(BaseModel):
    application_id: str
    status:         str
    cibil:          CibilScoreBreakdown
    result_message: CibilResultMessage
    hard_stop:      Optional[HardStopResponse] = None  # set if score < 650
    message:        str
    next_step:      str


# =============================================================================
# Step 6 — AA Init
# =============================================================================

class AAInitRequest(BaseModel):
    """
    Mizan Phase 04: AA may use a different mobile than personal mobile.
    This mobile is linked to the borrower's business current account.
    """
    bank_mobile: str = Field(
        ...,
        description="Mobile number registered with business current account bank",
    )

    @field_validator("bank_mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Invalid bank mobile number")
        return v


class AAInitResponse(BaseModel):
    application_id: str
    aa_client_id:   str
    status:         str
    redirect_url:   Optional[str] = None
    message:        str
    next_step:      str


# =============================================================================
# Step 7 — AA Fetch
# =============================================================================

class BankStatementSummary(BaseModel):
    transaction_count:   int
    months_of_data:      int
    date_range_from:     Optional[str] = None
    date_range_to:       Optional[str] = None
    total_credit_inflow: float
    active_credit_days:  int
    has_historical_emi:  bool    # True = engine detected EMI/OD patterns
    historical_emi_amt:  float   # estimated monthly amount from historical patterns


class AAFetchResponse(BaseModel):
    application_id:      str
    status:              str
    bank_summary:        BankStatementSummary
    # Set if historical EMI/OD detected — triggers Mizan's conditional question
    emi_confirmation_required: bool
    message:             str
    next_step:           str


# =============================================================================
# Step 7b — EMI/OD Confirmation (conditional — Mizan Phase 04)
# =============================================================================

class EMIODConfirmRequest(BaseModel):
    """
    Only asked when the engine detects historical EMI/OD patterns
    not fully captured in CIBIL. Mizan asks: 'Are these settled?'
    """
    settled: bool = Field(
        ...,
        description="True = borrower confirms EMI/OD fully settled. False = still active.",
    )


class EMIODConfirmResponse(BaseModel):
    application_id:  str
    settled:         bool
    status:          str
    message:         str
    next_step:       str


# =============================================================================
# Step 8 — Process: Safe Borrowing Limit + Lender Matching
# =============================================================================

class EngineMetrics(BaseModel):
    """Full set of deterministic engine outputs (PDF spec)."""
    total_credit_inflow:          float
    active_days:                  int
    detected_existing_emi:        float   # effective EMI from AA bank statement

    abb_daily:                    float
    bto_monthly_avg:              float

    median_monthly_flow:          float
    std_dev:                      float
    volatility_index:             float
    volatility_interpretation:    str

    revenue_concentration_pct:    float
    concentration_interpretation: str

    qoq_pct:                      float
    active_days_ratio:            float
    active_days_interpretation:   str

    operating_buffer:             float
    survival_surplus:             float
    base_safe_emi:                float

    volatility_multiplier:        float
    concentration_multiplier:     float
    vintage_multiplier:           float
    qoq_multiplier:               float
    combined_risk_multiplier:     float
    emi_after_penalties:          float

    stress_inflow:                float
    stress_operating_buffer:      float
    stress_survival_surplus:      float
    stress_emi:                   float

    final_safe_emi:               float
    risk_band:                    str
    tenure_multiplier:            int
    safe_loan_amount:             float


class EMITransaction(BaseModel):
    transaction_id: str
    amount:         float
    narration:      str
    emi_lender:     Optional[str]


class SafeBorrowingLimit(BaseModel):
    """
    Capaxis Product 1 — how much the borrower can safely borrow.
    Shown in Mizan Phase 05.
    """
    safe_loan_amount:  float
    monthly_emi:       float    # = final_safe_emi
    tenure_months:     int
    risk_band:         str

    # Mizan result card fields
    avg_monthly_inflow:  float   # shown in the card summary line
    existing_emi:        float   # shown in the card summary line
    is_target_achievable: bool   # True if safe_loan_amount >= target_loan_amount

    engine_metrics:    EngineMetrics
    claude_insights:   list[str]   # 5 bullets for "Why This Amount?" screen
    detected_emi_transactions: list[EMITransaction]

    # Mizan mandatory disclaimer
    disclaimer: str = "This is not a guaranteed approval. This is the amount we recommend you apply with."


class LenderRuleDetail(BaseModel):
    rule:      str
    passed:    bool
    reason:    Optional[str]
    value:     Optional[object]
    threshold: Optional[object]
    stage:     Optional[str] = None
    skipped:   bool = False


class LenderMatchResult(BaseModel):
    """Capaxis Product 2 — one lender's eligibility result."""
    lender_name:       str
    likely_to_approve: bool
    fail_reason:       Optional[str]
    all_fail_reasons:  list[str]
    pass_count:        int
    fail_count:        int
    rule_details:      list[LenderRuleDetail]


class LenderMatchingSummary(BaseModel):
    """Capaxis Product 2 — full lender matching result."""
    eligible_lenders:   list[str]
    ineligible_lenders: list[str]
    results:            list[LenderMatchResult]
    lender_match_insight: str  # Claude's 1-sentence summary for this section


class ProcessApplicationResponse(BaseModel):
    """
    Step 8 combined response.
    safe_borrowing_limit → shown in chat (Phase 05)
    lender_matching      → shown on separate results screen
    """
    application_id:       str
    borrower_name:        str
    loan_type:            str
    target_loan_amount:   float
    status:               str
    processing_time_ms:   float

    safe_borrowing_limit: SafeBorrowingLimit
    lender_matching:      LenderMatchingSummary


# =============================================================================
# Query schemas
# =============================================================================

class ApplicationStatusResponse(BaseModel):
    application_id:      str
    loan_type:           Optional[str]
    target_loan_amount:  Optional[float]
    status:              str
    hard_stop_code:      Optional[str]
    created_at:          str
    updated_at:          str
    cibil_consent:       Optional[str]
    aa_consent:          Optional[str]
    aa_client_id:        Optional[str]
    safe_loan_amount:    Optional[float]
    risk_band:           Optional[str]
    failure_reason:      Optional[str]


class AuditLogSchema(BaseModel):
    id:         str
    event:      str
    old_status: Optional[str]
    new_status: Optional[str]
    actor:      str
    created_at: str
    metadata:   Optional[dict]


class BorrowerProfileResponse(BaseModel):
    borrower_id:             str
    name:                    str
    gstin:                   str
    pan:                     str
    individual_pan:          Optional[str]
    cin:                     Optional[str]
    mobile:                  str
    email:                   Optional[str]
    gender:                  Optional[str]
    age:                     Optional[int]
    date_of_incorporation:   Optional[str]
    date_of_birth:           Optional[str]   # YYYY-MM-DD
    business_name:           Optional[str]
    business_nature:         Optional[str]
    business_industry:       Optional[str]
    business_product:        Optional[str]
    business_vintage_months: Optional[int]
    commercial_premises:     Optional[str]
    residence_premises:      Optional[str]
    pincode:                 Optional[str]
    whatsapp_number:         Optional[str]
    has_current_account:     Optional[bool]
    aa_bank_mobile:          Optional[str]   # bank account mobile (may differ from personal)
    profile_complete:        bool
    missing_fields:          list[str]
    cibil_consent:           Optional[str]
    cibil_consent_at:        Optional[str]   # ISO 8601 timestamp
    aa_consent:              Optional[str]
    aa_consent_at:           Optional[str]   # ISO 8601 timestamp
    created_at:              str


class BorrowerProfile(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    pan: str = Field(..., pattern=r"^[A-Za-z]{5}[0-9]{4}[A-Za-z]$")
    mobile: str = Field(..., description="10-digit mobile starting with 6-9")
    gender: Optional[str] = Field(None, pattern="^(male|female|other)$")
    age: int = Field(..., ge=18, le=80)
    business_vintage_months: int = Field(..., ge=0)
    business_industry: str
    commercial_premises: str = Field(..., pattern="^(Owned|Rented|Partially Owned)$")
    residence_premises: str = Field(..., pattern="^(Owned|Rented|Partially Owned)$")
    pincode: str = Field(..., pattern=r"^\d{6}$")

    @field_validator("pan")
    @classmethod
    def uppercase_profile_pan(cls, v: str) -> str:
        return v.upper()

    @field_validator("mobile")
    @classmethod
    def validate_profile_mobile(cls, v: str) -> str:
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Invalid Indian mobile — must be 10 digits starting with 6-9")
        return v
