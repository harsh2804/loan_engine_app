"""
database/models.py
──────────────────
SQLAlchemy ORM models aligned with the Mizan conversation flow.

Mizan Phase 1 pipeline:
  Phase 01  Greeting + Loan Type selection
  Phase 02  Hard Gate Qualification (9 gates)
            Gate 5  = Hard Stop A (no current account)
  Phase 03  CIBIL consent → OTP → fetch
            Hard Stop B   = CIBIL score < 650
  Phase 04  AA consent → OTP → init → fetch → EMI/OD confirmation
  Phase 05  Results — Safe Borrowing Limit + Lender Matching

Application lifecycle:
  PROFILE_SAVED        borrower profile + gates stored
  CIBIL_CONSENT_GIVEN  explicit consent recorded
  CIBIL_OTP_SENT       OTP sent to borrower's registered mobile
  CIBIL_FETCHED        report pulled, score ≥ 650
  AA_CONSENT_GIVEN     explicit consent recorded
  AA_INIT_DONE         session started, client_id stored
  AA_FETCHED           bank statement pulled
  PROCESSING           engine + Claude running
  COMPLETED            both products delivered
  FAILED               any step failed (hard stops also write FAILED)

Hard Stops (stored in hard_stop_reason):
  HARD_STOP_A  no current account in business name
  HARD_STOP_B  CIBIL score below 650
"""
from __future__ import annotations
import enum as py_enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, JSON, Enum as SAEnum, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# =============================================================================
# Python enums
# =============================================================================

class GenderEnum(str, py_enum.Enum):
    male   = "male"
    female = "female"
    other  = "other"


class PremisesEnum(str, py_enum.Enum):
    owned           = "Owned"
    rented          = "Rented"
    partially_owned = "Partially Owned"


class BusinessNatureEnum(str, py_enum.Enum):
    retailer           = "Retailer"
    wholesaler         = "Wholesaler"
    wholesaler_retailer = "Wholesaler & Retailer"
    manufacturer       = "Manufacturer"
    service_provider   = "Service Provider"
    trader             = "Trader"


class LoanTypeEnum(str, py_enum.Enum):
    unsecured_term_loan = "Unsecured Term Loan"
    secured_term_loan   = "Secured Term Loan"


class ApplicationStatusEnum(str, py_enum.Enum):
    profile_saved        = "PROFILE_SAVED"
    cibil_consent_given  = "CIBIL_CONSENT_GIVEN"
    cibil_otp_sent       = "CIBIL_OTP_SENT"
    cibil_fetched        = "CIBIL_FETCHED"
    aa_consent_given     = "AA_CONSENT_GIVEN"
    aa_consent_completed = "AA_CONSENT_COMPLETED"
    aa_init_done         = "AA_INIT_DONE"
    aa_fetched           = "AA_FETCHED"
    processing           = "PROCESSING"
    completed            = "COMPLETED"
    failed               = "FAILED"


class RiskBandEnum(str, py_enum.Enum):
    low    = "Low Risk"
    medium = "Medium Risk"
    high   = "High Risk"


class ApiServiceEnum(str, py_enum.Enum):
    cibil          = "CIBIL"
    aa_init        = "AA_INIT"
    aa_fetch       = "AA_FETCH"
    gst_verify     = "GST_VERIFY"
    mca_gstin_to_cin = "MCA_GSTIN_TO_CIN"
    claude         = "CLAUDE"
    claude_summary = "CLAUDE_SUMMARY"


class CreditCategoryEnum(str, py_enum.Enum):
    revenue      = "Revenue"
    loan_inward  = "Loan Inward"
    own_transfer = "Own Transfer"
    cash_deposit = "Cash Deposit"


class TxnTypeEnum(str, py_enum.Enum):
    credit = "CREDIT"
    debit  = "DEBIT"


# =============================================================================
# SAEnum helper
# =============================================================================

def _pg_enum(py_enum_cls: type[py_enum.Enum], pg_name: str) -> SAEnum:
    return SAEnum(
        py_enum_cls,
        name=pg_name,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


_GENDER_COL       = _pg_enum(GenderEnum,            "gender_type")
_PREMISES_COL     = _pg_enum(PremisesEnum,           "premises_type")
_BIZ_NATURE_COL   = _pg_enum(BusinessNatureEnum,     "business_nature_type")
_LOAN_TYPE_COL    = _pg_enum(LoanTypeEnum,           "loan_type")
_STATUS_COL       = _pg_enum(ApplicationStatusEnum,  "application_status")
_RISK_BAND_COL    = _pg_enum(RiskBandEnum,           "risk_band_type")
_SERVICE_COL      = _pg_enum(ApiServiceEnum,         "api_service_type")
_CREDIT_CAT_COL   = _pg_enum(CreditCategoryEnum,     "credit_category_type")
_TXN_TYPE_COL     = _pg_enum(TxnTypeEnum,            "txn_type")


# =============================================================================
# Base + TimestampMixin
# =============================================================================

class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


# =============================================================================
# Borrowers
# =============================================================================

class Borrower(TimestampMixin, Base):
    """
    One row per borrower.
    PAN/GSTIN/CIN/date_of_incorporation are stored in `signups` and linked 1:1.
    Fields tagged SAVED_ONCE are collected during first Mizan session and
    never asked again. The API skips re-collecting them if already populated.
    """
    __tablename__ = "borrowers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # ── Core profile (identity fields live in `signups`) ──────────────────────
    name:          Mapped[str]           = mapped_column(String(200), nullable=False)
    mobile:        Mapped[str]           = mapped_column(String(15),  nullable=False)
    email:         Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    gender:        Mapped[Optional[str]] = mapped_column(_GENDER_COL, nullable=True)
    age:           Mapped[Optional[int]] = mapped_column(Integer,     nullable=True)
    # Collected during Mizan Phase 03 CIBIL input collection — format YYYY-MM-DD
    date_of_birth: Mapped[Optional[str]] = mapped_column(String(10),  nullable=True)

    # Individual PAN (for CIBIL) â€” separate from company PAN stored in `signups`
    individual_pan: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # ── Business profile (SAVED_ONCE) ─────────────────────────────────────────
    business_name:              Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    business_nature:            Mapped[Optional[str]] = mapped_column(_BIZ_NATURE_COL, nullable=True)
    business_industry:          Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    business_product:           Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    business_vintage_months:    Mapped[Optional[int]] = mapped_column(Integer,     nullable=True)
    commercial_premises:        Mapped[Optional[str]] = mapped_column(_PREMISES_COL, nullable=True)
    residence_premises:         Mapped[Optional[str]] = mapped_column(_PREMISES_COL, nullable=True)
    pincode:                    Mapped[Optional[str]] = mapped_column(String(10),  nullable=True)

    # ── Contact (SAVED_ONCE) ──────────────────────────────────────────────────
    # mobile above = primary contact number
    # whatsapp_number: if same as mobile → store mobile again; if different → store separately
    whatsapp_number:            Mapped[Optional[str]] = mapped_column(String(15),  nullable=True)

    # ── Gate 5 hard-stop flag (SAVED_ONCE) ────────────────────────────────────
    # True  = confirmed current account exists → proceed
    # False = no current account → Hard Stop A
    has_current_account:        Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # ── Consent tracking ──────────────────────────────────────────────────────
    cibil_consent:    Mapped[Optional[str]]      = mapped_column(String(1),              nullable=True)
    cibil_consent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    aa_consent:       Mapped[Optional[str]]      = mapped_column(String(1),              nullable=True)
    aa_consent_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── AA bank mobile (may differ from personal mobile) ─────────────────────
    aa_bank_mobile:   Mapped[Optional[str]] = mapped_column(String(15), nullable=True)

    signup: Mapped[Optional["Signup"]] = relationship(
        back_populates="borrower",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    applications: Mapped[list["LoanApplication"]] = relationship(
        back_populates="borrower", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_borrowers_mobile", "mobile"),
    )

    def _require_signup(self) -> "Signup":
        if self.signup is None:
            raise RuntimeError(f"Missing signup row for borrower_id={self.id}")
        return self.signup

    @property
    def gstin(self) -> str:
        return self._require_signup().gstin

    @property
    def pan(self) -> str:
        return self._require_signup().pan

    @property
    def cin(self) -> Optional[str]:
        return self._require_signup().cin

    @property
    def date_of_incorporation(self) -> Optional[str]:
        return self._require_signup().date_of_incorporation

    @property
    def profile_complete(self) -> bool:
        """True when all SAVED_ONCE fields have been collected."""
        return all([
            self.business_nature,
            self.business_industry,
            self.business_product,
            self.commercial_premises,
            self.residence_premises,
            self.has_current_account is not None,
        ])

    @property
    def missing_profile_fields(self) -> list[str]:
        """Returns list of SAVED_ONCE field names still missing."""
        fields = []
        if not self.business_nature:       fields.append("business_nature")
        if not self.business_industry:     fields.append("business_industry")
        if not self.business_product:      fields.append("business_product")
        if not self.commercial_premises:   fields.append("commercial_premises")
        if not self.residence_premises:    fields.append("residence_premises")
        if self.has_current_account is None: fields.append("has_current_account")
        return fields


# =============================================================================
# Signups (PAN/GSTIN/CIN)
# =============================================================================

class Signup(TimestampMixin, Base):
    """
    Stores PAN/GSTIN/CIN, GST profile details, and incorporation date.
    One row per borrower (1:1).
    """
    __tablename__ = "signups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # May be null before the borrower completes signup (GST identity verified first).
    borrower_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("borrowers.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )

    gstin: Mapped[str] = mapped_column(String(15), nullable=False, unique=True)
    pan:   Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    business_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    constitution: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    trade_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cin:   Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    date_of_incorporation: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    borrower: Mapped["Borrower"] = relationship(back_populates="signup")

    __table_args__ = (
        Index("ix_signups_borrower_id", "borrower_id"),
        Index("ix_signups_gstin", "gstin"),
        Index("ix_signups_pan", "pan"),
    )


# =============================================================================
# Loan Applications
# =============================================================================

class LoanApplication(TimestampMixin, Base):
    __tablename__ = "loan_applications"

    id:          Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    borrower_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("borrowers.id"), nullable=False
    )

    # ── Loan intent (from Phase 01) ───────────────────────────────────────────
    loan_type:          Mapped[Optional[str]]   = mapped_column(_LOAN_TYPE_COL, nullable=True)
    target_loan_amount: Mapped[Optional[float]] = mapped_column(Float,          nullable=True)

    # ── Pipeline status ───────────────────────────────────────────────────────
    status:         Mapped[str]           = mapped_column(_STATUS_COL, nullable=False,
                                                          default="PROFILE_SAVED")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Hard stop codes: HARD_STOP_A | HARD_STOP_B | null
    hard_stop_code:    Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    hard_stop_detail:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── External API IDs ──────────────────────────────────────────────────────
    cibil_client_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    aa_client_id:    Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # ── Staged data ───────────────────────────────────────────────────────────
    cibil_summary:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    bank_metrics:    Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    engine_output:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Conditional EMI/OD confirmation (Phase 04) ───────────────────────────
    # None = question not yet asked | True = borrower confirmed settled
    # False = still active (increases effective EMI)
    emi_od_settled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # ── Final outputs ─────────────────────────────────────────────────────────
    safe_loan_amount:   Mapped[Optional[float]] = mapped_column(Float,          nullable=True)
    risk_band:          Mapped[Optional[str]]   = mapped_column(_RISK_BAND_COL, nullable=True)
    claude_summary:     Mapped[Optional[list]]  = mapped_column(JSON,           nullable=True)
    processing_time_ms: Mapped[Optional[float]] = mapped_column(Float,          nullable=True)

    borrower:           Mapped["Borrower"]              = relationship(back_populates="applications")
    api_logs:           Mapped[list["ApiCallLog"]]       = relationship(back_populates="application")
    audit_logs:         Mapped[list["AuditLog"]]         = relationship(back_populates="application")
    lender_decisions:   Mapped[list["LenderDecision"]]   = relationship(back_populates="application")
    transaction_labels: Mapped[list["TransactionLabel"]] = relationship(back_populates="application")

    __table_args__ = (
        Index("ix_applications_borrower",   "borrower_id"),
        Index("ix_applications_status",     "status"),
        Index("ix_applications_created_at", "created_at"),
        Index("ix_applications_hard_stop",  "hard_stop_code"),
    )


# =============================================================================
# API Call Logs — append only
# =============================================================================

class ApiCallLog(TimestampMixin, Base):
    __tablename__ = "api_call_logs"

    id:             Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("loan_applications.id"), nullable=False
    )
    service:  Mapped[str] = mapped_column(_SERVICE_COL, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(300),  nullable=False)
    method:   Mapped[str] = mapped_column(String(10),   nullable=False)
    request_body:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status_code:    Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    duration_ms:    Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    success:        Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False)
    error_message:  Mapped[Optional[str]]   = mapped_column(Text,    nullable=True)
    attempt_number: Mapped[int]             = mapped_column(Integer, nullable=False, default=1)

    application: Mapped["LoanApplication"] = relationship(back_populates="api_logs")

    __table_args__ = (
        Index("ix_api_logs_application", "application_id"),
        Index("ix_api_logs_service",     "service"),
    )


# =============================================================================
# Audit Logs — append only
# =============================================================================

class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"

    id:             Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("loan_applications.id"), nullable=False
    )
    event:      Mapped[str]            = mapped_column(String(100), nullable=False)
    old_status: Mapped[Optional[str]]  = mapped_column(String(30),  nullable=True)
    new_status: Mapped[Optional[str]]  = mapped_column(String(30),  nullable=True)
    actor:      Mapped[str]            = mapped_column(String(50),  nullable=False, default="system")
    extra_metadata:   Mapped[Optional[dict]] = mapped_column(JSON,        nullable=True)

    application: Mapped["LoanApplication"] = relationship(back_populates="audit_logs")

    __table_args__ = (
        Index("ix_audit_logs_application", "application_id"),
        Index("ix_audit_logs_event",       "event"),
    )


# =============================================================================
# Lender Decisions
# =============================================================================

class LenderDecision(TimestampMixin, Base):
    __tablename__ = "lender_decisions"

    id:             Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("loan_applications.id"), nullable=False
    )
    lender_name:  Mapped[str]            = mapped_column(String(100), nullable=False)
    eligible:     Mapped[bool]           = mapped_column(Boolean,     nullable=False)
    fail_reason:  Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)
    rule_details: Mapped[Optional[dict]] = mapped_column(JSON,        nullable=True)

    application: Mapped["LoanApplication"] = relationship(back_populates="lender_decisions")

    __table_args__ = (
        Index("ix_lender_decisions_application", "application_id"),
        Index("ix_lender_decisions_lender",      "lender_name"),
    )


# =============================================================================
# Transaction Labels
# =============================================================================

class TransactionLabel(TimestampMixin, Base):
    # It looks like the code you provided is a comment in Python. Comments in Python start with a hash
    # symbol (#) and are used to provide explanations or notes within the code. In this case, the
    # comment appears to be incomplete as it ends abruptly with "__tab".
    __tablename__ = "transaction_labels"

    id:             Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("loan_applications.id"), nullable=False
    )
    transaction_id: Mapped[str]   = mapped_column(String(100),    nullable=False)
    amount:         Mapped[float] = mapped_column(Float,           nullable=False)
    narration:      Mapped[str]   = mapped_column(Text,            nullable=False)
    txn_type:       Mapped[str]   = mapped_column(_TXN_TYPE_COL,  nullable=False)
    credit_category:   Mapped[Optional[str]]  = mapped_column(_CREDIT_CAT_COL, nullable=True)
    is_emi_obligation: Mapped[Optional[bool]] = mapped_column(Boolean,          nullable=True)
    emi_lender:        Mapped[Optional[str]]  = mapped_column(String(100),       nullable=True)

    application: Mapped["LoanApplication"] = relationship(back_populates="transaction_labels")

    __table_args__ = (
        Index("ix_txn_labels_application", "application_id"),
        Index("ix_txn_labels_txn_id",      "transaction_id"),
    )
