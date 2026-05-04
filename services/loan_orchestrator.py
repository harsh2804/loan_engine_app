"""
services/loan_orchestrator.py
──────────────────────────────
Capaxis/Mizan staged loan pipeline orchestrator.

Mizan Flow → Method Map:
  Phase 01-02  register_borrower()       Step 1
  Phase 01-02  start_application()       Step 2
  Phase 03     record_cibil_consent()    Step 3
  Phase 03     fetch_cibil()             Step 4  (Hard Stop B enforced here)
  Phase 04     record_aa_consent()       Step 5
  Phase 04     init_aa()                 Step 6
  Phase 04     fetch_aa()                Step 7
  Phase 04     confirm_emi_od()          Step 7b (conditional)
  Phase 05     process_application()     Step 8

Hard Stops:
  HARD_STOP_A — no current account (Gate 5, enforced in register_borrower)
  HARD_STOP_B — CIBIL score < 650   (enforced in fetch_cibil)

Saved-once semantics:
  If a borrower already has business_nature, commercial_premises etc. stored,
  the registration flow skips collecting them again (profile_complete=True).
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Optional

from database.repositories.repositories import UnitOfWork
from lenders.engine import LenderContext
from lenders.registry import registry
from models.schemas import (
    GstinVerifyResponse, SignupPage1Request,
    BorrowerRegisterRequest, BorrowerRegisterResponse,
    ApplicationStartResponse,
    ConsentResponse,
    CibilScoreBreakdown, CibilResultMessage, CibilFetchResponse,
    AAInitResponse, AACompleteResponse, AAFetchResponse, BankStatementSummary,
    EMIODConfirmResponse,
    ProcessApplicationResponse, SafeBorrowingLimit, EngineMetrics, OriginalRequestImpact,
    LenderMatchingSummary, LenderMatchResult, LenderRuleDetail, EMITransaction,
    HardStopResponse,
)
from services.external.aa_client    import AccountAggregatorClient
from services.external.cibil_client import CibilClient
from services.external.gst_client import GstVerificationClient
from services.external.itr_client import ItrTurnoverClient
from services.external.mca_client import McaGstinClient
from services.transaction_classifier import classify_transactions, build_classification_index
from services.loan_engine   import (
    run_engine,
    banking_turnover_ratio_pct,
    emi_from_loan_amount,
    requested_loan_risk_level,
 )
from services.existing_emi import (
    bank_emi_items_from_transactions,
    bureau_emi_items_from_cibil_accounts,
    compute_existing_emi,
)
from services.summarizer    import generate_decision_summary
from utils.aa_parser        import (
    parse_aa_payload, aggregate_monthly_credits,
    compute_daily_balances, count_active_days,
)
from utils.cibil_parser import parse_cibil_payload
from utils.itr_parser import parse_itr_turnover_payload


# ── Constants ─────────────────────────────────────────────────────────────────

CIBIL_HARD_STOP_THRESHOLD = 650   # Hard Stop B — must match Mizan spec


# ── Status transition guards ──────────────────────────────────────────────────

_ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "CIBIL_CONSENT_GIVEN": ["PROFILE_SAVED",        "CIBIL_CONSENT_GIVEN"],
    "CIBIL_FETCHED":       ["CIBIL_CONSENT_GIVEN"],
    "AA_CONSENT_GIVEN":    ["CIBIL_FETCHED",         "AA_CONSENT_GIVEN"],
    "AA_INIT_DONE":        ["AA_CONSENT_GIVEN"],
    "AA_CONSENT_COMPLETED": ["AA_INIT_DONE", "AA_CONSENT_COMPLETED"],
    "AA_FETCHED":          ["AA_CONSENT_COMPLETED"],
    "PROCESSING":          ["AA_FETCHED"],
}


def _guard(current: str, target: str, step: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(target, [])
    if current not in allowed:
        raise ValueError(
            f"Cannot execute '{step}': application is '{current}'. "
            f"Required: one of {allowed}."
        )


def _score_interpretation(score: int) -> str:
    if score >= 750: return "Excellent"
    if score >= 700: return "Good"
    if score >= 650: return "Fair"
    return                 "Poor"


# ── Hard Stop builders ────────────────────────────────────────────────────────

def _hard_stop_a() -> HardStopResponse:
    return HardStopResponse(
        code     = "HARD_STOP_A",
        reason   = "No current account found in business name.",
        guidance = (
            "All lenders on our platform require a current account in your business name "
            "to verify your turnover and banking history. Open a current account with any "
            "bank in your business name. Once it has at least 6 months of transaction history, "
            "come back and Mizan will run the full analysis."
        ),
        cta_label = "Start fresh analysis",
    )


def _hard_stop_b(score: int, overdue: float) -> HardStopResponse:
    guidance = (
        f"Your current CIBIL score of {score} is below the minimum threshold of "
        f"{CIBIL_HARD_STOP_THRESHOLD} required by lenders on our platform. "
    )
    if overdue > 0:
        guidance += (
            f"Clear the overdue amount of ₹{overdue:,.0f} as soon as possible — "
            "overdue amounts have the biggest impact on your score. "
        )
    guidance += (
        "Scores generally begin recovering within 60–90 days after clearing overdues. "
        "Avoid applying for any new loans or credit cards for the next 3–6 months. "
        f"Come back once your score crosses {CIBIL_HARD_STOP_THRESHOLD}."
    )
    return HardStopResponse(
        code      = "HARD_STOP_B",
        reason    = f"CIBIL score {score} is below the minimum threshold of {CIBIL_HARD_STOP_THRESHOLD}.",
        guidance  = guidance,
        cta_label = "Start fresh analysis",
    )


def _cibil_result_message(cibil: dict) -> CibilResultMessage:
    """Build Mizan-aligned result message (clean vs issues variant)."""
    score   = cibil["score"]
    overdue = cibil["overdue_amount"]
    dpd     = cibil["max_days_overdue"]

    if overdue == 0 and dpd == 0:
        return CibilResultMessage(
            variant  = "clean",
            headline = "Your credit profile is clean.",
            detail   = (
                f"A score of {score} with no overdue amounts and no missed payments "
                "puts you in a strong position with most lenders. Let's move to your bank statement now."
            ),
        )
    return CibilResultMessage(
        variant  = "issues",
        headline = f"Your credit score of {score} is above the minimum threshold.",
        detail   = (
            f"However, you have an overdue amount of ₹{overdue:,.0f} "
            f"and a payment delay of {dpd} days on record. "
            "This has been factored into your borrowing limit."
        ),
    )


class LoanOrchestrator:

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # =========================================================================
    # Step 1 — Register / Update Borrower
    # =========================================================================

    async def verify_gstin_for_signup(
        self,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
    ) -> GstinVerifyResponse:
        """
        Verifies GSTIN via external services and stores the identity fields in `signups`.
        This can happen before a borrower row exists.
        """
        uow = self._uow
        gstin = gstin.upper()
        identity = await self._resolve_borrower_identity(
            gstin, fetch_filings=fetch_filings, fy=fy
        )

        payload = {
            "gstin": gstin,
            "pan": identity["pan"].upper(),
            "business_name": identity.get("business_name"),
            "constitution": identity.get("constitution"),
            "trade_name": identity.get("trade_name"),
            "address": identity.get("address"),
            "cin": identity.get("cin"),
            "date_of_incorporation": identity.get("date_of_incorporation"),
        }

        existing = await uow.signups.get_by_gstin(gstin)
        if existing:
            signup = await uow.signups.update(existing.id, **payload)
        else:
            signup = await uow.signups.create(borrower_id=None, **payload)

        if signup is None:
            raise RuntimeError("Failed to persist signup row.")

        return GstinVerifyResponse(
            signup_id=signup.id,
            gstin=signup.gstin,
            pan=signup.pan,
            business_name=signup.business_name,
            constitution=signup.constitution,
            trade_name=signup.trade_name,
            address=signup.address,
            cin=signup.cin,
            date_of_incorporation=signup.date_of_incorporation,
            next_step='POST /api/v1/signup/page-1  { "signup_id": "<id>", "name": "...", "mobile": "...", "gender": "male|female|other", "date_of_birth": "YYYY-MM-DD", "individual_pan": "ABCDE1234F" }',
        )

    async def complete_signup_page_1(self, req: SignupPage1Request) -> BorrowerRegisterResponse:
        """
        Creates/updates a borrower record from Page-1 signup info and links it to the verified signup row.
        Identity fields are never taken from the request.
        """
        uow = self._uow

        signup = await uow.signups.get_by_id(req.signup_id)
        if not signup:
            raise ValueError(f"Signup '{req.signup_id}' not found. Verify GSTIN first.")

        if req.company_pan and req.company_pan.upper() != signup.pan.upper():
            raise ValueError("Company PAN does not match the verified GSTIN record.")

        borrower_updates: dict[str, Any] = {
            "name": req.name,
            "mobile": req.mobile,
            "gender": req.gender,
            "date_of_birth": req.date_of_birth,
            "individual_pan": req.individual_pan,
            "whatsapp_number": req.mobile,
        }
        borrower_updates = {k: v for k, v in borrower_updates.items() if v is not None}

        gst_profile = await self._fetch_business_profile_from_gstin(signup.gstin)
        if gst_profile.get("business_nature"):
            borrower_updates.setdefault("business_nature", gst_profile["business_nature"])
        if gst_profile.get("business_name"):
            borrower_updates.setdefault("business_name", gst_profile["business_name"])

        is_new = False
        if signup.borrower_id:
            borrower = await uow.borrowers.get_by_id(signup.borrower_id)
            if not borrower:
                raise RuntimeError(f"Signup row references missing borrower_id={signup.borrower_id}")
            if borrower.business_nature:
                borrower_updates.pop("business_nature", None)
            await uow.borrowers.update(borrower.id, **borrower_updates)
            borrower = await uow.borrowers.get_by_id(borrower.id)
        else:
            borrower = await uow.borrowers.create(**borrower_updates)
            await uow.signups.update(signup.id, borrower_id=borrower.id)
            borrower = await uow.borrowers.get_by_id(borrower.id)
            is_new = True

        if borrower is None:
            raise RuntimeError("Failed to load borrower after signup page 1.")

        borrower = await self._ensure_business_vintage_months(
            borrower,
            date_of_incorporation=signup.date_of_incorporation,
        )

        hard_stop = _hard_stop_a() if borrower.has_current_account is False else None
        missing = borrower.missing_profile_fields

        if hard_stop:
            next_step = "Hard Stop A â€” cannot proceed. See hard_stop.guidance for next steps."
        elif missing:
            next_step = f"POST /api/v1/borrowers/register  {{ \"borrower_id\": \"{borrower.id}\", ...missing fields... }}"
        else:
            next_step = (
                f"POST /api/v1/loan/applications/start  "
                f'{{ "borrower_pan": "{borrower.pan}", "loan_type": "Unsecured Term Loan", "target_loan_amount": <amount> }}'
            )

        return BorrowerRegisterResponse(
            borrower_id      = borrower.id,
            gstin            = borrower.gstin,
            pan              = borrower.pan,
            individual_pan   = borrower.individual_pan,
            cin              = borrower.cin,
            date_of_incorporation = borrower.date_of_incorporation,
            name             = borrower.name,
            is_new           = is_new,
            profile_complete = borrower.profile_complete,
            missing_fields   = missing,
            hard_stop        = hard_stop,
            message          = "Signup details saved." if is_new else "Signup details updated.",
            next_step        = next_step,
        )

    async def register_borrower(
        self, req: BorrowerRegisterRequest,
    ) -> BorrowerRegisterResponse:
        uow = self._uow

        borrower = None
        is_new = False
        signup_date_of_incorporation = None

        # Build update dict — only include fields that were actually provided
        profile_fields: dict = {}
        for field in [
            "name", "mobile", "email", "gender", "age", "date_of_birth",
            "business_name", "business_nature", "business_industry",
            "business_product", "business_vintage_months",
            "commercial_premises", "residence_premises", "pincode",
        ]:
            val = getattr(req, field, None)
            if val is not None:
                profile_fields[field] = val

        # Handle whatsapp: if not explicitly provided, default to mobile
        if req.whatsapp_number is not None:
            profile_fields["whatsapp_number"] = req.whatsapp_number
        elif req.mobile:
            profile_fields.setdefault("whatsapp_number", req.mobile)

        if req.has_current_account is not None:
            profile_fields["has_current_account"] = req.has_current_account

        # Identity fields (GSTIN/PAN/CIN/DOI) must come from the signup table.
        if req.borrower_id:
            borrower = await uow.borrowers.get_by_id(req.borrower_id)
            if not borrower:
                raise ValueError(f"Borrower '{req.borrower_id}' not found.")

            signup = await uow.signups.get_by_borrower_id(borrower.id)
            if not signup:
                raise RuntimeError(f"Missing signup row for borrower_id={borrower.id}")

            if req.gstin and req.gstin.upper() != signup.gstin.upper():
                raise ValueError("gstin does not match borrower_id's signup record.")

            signup_date_of_incorporation = signup.date_of_incorporation

            if profile_fields:
                await uow.borrowers.update(borrower.id, **profile_fields)
            borrower = await uow.borrowers.get_by_id(borrower.id)
        else:
            if not req.gstin:
                raise ValueError("gstin is required when borrower_id is not provided.")

            signup = await uow.signups.get_by_gstin(req.gstin)
            if signup:
                if signup.borrower_id:
                    borrower = await uow.borrowers.get_by_id(signup.borrower_id)
                    if not borrower:
                        raise RuntimeError(f"Signup row exists for gstin={req.gstin} but borrower is missing.")
                else:
                    # GSTIN was verified earlier but the borrower hasn't completed signup yet.
                    borrower = await uow.borrowers.create(**profile_fields)
                    await uow.signups.update(signup.id, borrower_id=borrower.id)
                    borrower = await uow.borrowers.get_by_id(borrower.id)
                    is_new = True

                signup_date_of_incorporation = signup.date_of_incorporation

                if profile_fields:
                    await uow.borrowers.update(borrower.id, **profile_fields)
                borrower = await uow.borrowers.get_by_id(borrower.id)
            else:
                # Legacy fallback: resolve identity from GSTIN and upsert signups.
                identity = await self._resolve_borrower_identity(req.gstin)
                signup_fields = {
                    "gstin": req.gstin,
                    "pan": identity["pan"].upper(),
                    "cin": identity.get("cin"),
                    "date_of_incorporation": identity.get("date_of_incorporation"),
                }
                signup_date_of_incorporation = identity.get("date_of_incorporation")

                existing = await uow.borrowers.get_by_pan(identity["pan"])
                if existing:
                    # Saved-once: only overwrite fields that were explicitly supplied
                    if profile_fields:
                        await uow.borrowers.update(existing.id, **profile_fields)

                    existing_signup = await uow.signups.get_by_borrower_id(existing.id)
                    if existing_signup:
                        await uow.signups.update(existing_signup.id, **signup_fields)
                    else:
                        await uow.signups.create(borrower_id=existing.id, **signup_fields)

                    borrower = await uow.borrowers.get_by_id(existing.id)
                    is_new = False
                else:
                    borrower = await uow.borrowers.create(**profile_fields)
                    await uow.signups.create(borrower_id=borrower.id, **signup_fields)
                    borrower = await uow.borrowers.get_by_id(borrower.id)
                    is_new = True

        if borrower is None:
            raise RuntimeError("Unexpected error: borrower resolution failed.")

        borrower = await self._ensure_business_vintage_months(
            borrower,
            date_of_incorporation=signup_date_of_incorporation,
            explicit_vintage_provided=profile_fields.get("business_vintage_months") is not None,
        )

        if borrower.business_nature is None:
            gst_business_nature = await self._fetch_business_nature_from_gstin(borrower.gstin)
            if gst_business_nature:
                await uow.borrowers.update(borrower.id, business_nature=gst_business_nature)
                borrower = await uow.borrowers.get_by_id(borrower.id)
        if borrower.business_name is None:
            gst_business_name = await self._fetch_business_name_from_gstin(borrower.gstin)
            if gst_business_name:
                await uow.borrowers.update(borrower.id, business_name=gst_business_name)
                borrower = await uow.borrowers.get_by_id(borrower.id)

        # Check Hard Stop A — enforced here before any application is created.
        # The hard_stop object is returned in the response; no application row exists yet.
        hard_stop = None
        if borrower.has_current_account is False:
            hard_stop = _hard_stop_a()

        missing = borrower.missing_profile_fields
        if hard_stop:
            next_step = "Hard Stop A — cannot proceed. See hard_stop.guidance for next steps."
        elif missing:
            next_step = f"Provide missing profile fields: {missing}"
        else:
            next_step = (
                f"POST /api/v1/loan/applications/start  "
                f'{{ "borrower_pan": "{borrower.pan}", "loan_type": "Unsecured Term Loan", "target_loan_amount": <amount> }}'
            )

        return BorrowerRegisterResponse(
            borrower_id      = borrower.id,
            gstin            = borrower.gstin,
            pan              = borrower.pan,
            individual_pan   = borrower.individual_pan,
            cin              = borrower.cin,
            date_of_incorporation = borrower.date_of_incorporation,
            name             = borrower.name,
            is_new           = is_new,
            profile_complete = borrower.profile_complete,
            missing_fields   = missing,
            hard_stop        = hard_stop,
            message          = (
                "Borrower registered successfully." if is_new
                else "Borrower profile updated."
            ),
            next_step = next_step,
        )

    # =========================================================================
    # Step 2 — Start Application
    # =========================================================================

    async def start_application(
        self,
        individual_pan:     str,
        loan_type:          str,
        target_loan_amount: float,
    ) -> ApplicationStartResponse:
        uow = self._uow

        borrower = await uow.borrowers.get_by_individual_pan(individual_pan)
        if not borrower:
            raise ValueError(
                f"Borrower '{individual_pan}' not found. "
                "Register via POST /api/v1/borrowers/register first."
            )

        # Hard Stop A enforced here too — cannot start without current account
        if borrower.has_current_account is False:
            raise ValueError(
                "HARD_STOP_A: Cannot start application — no current account on record. "
                "Update the borrower profile with has_current_account=true to proceed."
            )

        if not borrower.profile_complete:
            missing = borrower.missing_profile_fields
            raise ValueError(
                f"Borrower profile incomplete. Missing fields: {missing}. "
                "Complete registration before starting an application."
            )

        app = await uow.applications.create(
            borrower_id        = borrower.id,
            status             = "PROFILE_SAVED",
            loan_type          = loan_type,
            target_loan_amount = target_loan_amount,
        )
        await uow.audit_logs.log_event(
            application_id=app.id, event="APPLICATION_STARTED",
            new_status="PROFILE_SAVED",
            metadata={
                "loan_type": loan_type,
                "target_amount": target_loan_amount,
            },
        )

        return ApplicationStartResponse(
            application_id     = app.id,
            borrower_id        = borrower.id,
            borrower_name      = borrower.name,
            loan_type          = loan_type,
            target_loan_amount = target_loan_amount,
            status             = "PROFILE_SAVED",
            message            = (
                f"Application created for {loan_type} of ₹{target_loan_amount:,.0f}. "
                "Collect CIBIL consent to proceed."
            ),
            next_step = (
                f"POST /api/v1/loan/applications/{app.id}/consent/cibil  "
                "{ \"consent\": \"Y\", \"ip_address\": \"<ip>\" }"
            ),
        )

    # =========================================================================
    # Step 3 — Record CIBIL Consent
    # =========================================================================

    async def record_cibil_consent(
        self,
        application_id: str,
        consent: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ConsentResponse:
        uow = self._uow
        app = await self._get_app(application_id)
        _guard(app.status, "CIBIL_CONSENT_GIVEN", "record_cibil_consent")

        now = datetime.now(timezone.utc)
        await uow.borrowers.update(
            app.borrower_id,
            cibil_consent=consent, cibil_consent_at=now,
        )
        await uow.applications.set_status(application_id, "CIBIL_CONSENT_GIVEN")
        await uow.audit_logs.log_event(
            application_id=application_id, event="CIBIL_CONSENT_RECORDED",
            old_status=app.status, new_status="CIBIL_CONSENT_GIVEN",
            metadata={"consent": consent, "ip": ip_address},
        )

        if consent == "N":
            return ConsentResponse(
                application_id=application_id, consent_type="CIBIL",
                consent_given=False, recorded_at=now.isoformat(),
                status="CIBIL_CONSENT_GIVEN",
                message="CIBIL consent withheld. Cannot proceed without consent.",
                next_step="Re-submit with consent='Y' when the borrower is ready.",
            )
        return ConsentResponse(
            application_id=application_id, consent_type="CIBIL",
            consent_given=True, recorded_at=now.isoformat(),
            status="CIBIL_CONSENT_GIVEN",
            message="CIBIL consent recorded. Ready to fetch credit report.",
            next_step=f"POST /api/v1/loan/applications/{application_id}/cibil/fetch",
        )

    # =========================================================================
    # Step 4 — Fetch CIBIL (with Hard Stop B)
    # =========================================================================

    async def fetch_cibil(self, application_id: str) -> CibilFetchResponse:
        uow      = self._uow
        app      = await self._get_app(application_id)
        _guard(app.status, "CIBIL_FETCHED", "fetch_cibil")
        borrower = await uow.borrowers.get_by_id(app.borrower_id)

        if (borrower.cibil_consent or "N") != "Y":
            raise ValueError("CIBIL consent not given. Call /consent/cibil first.")
        if not borrower.individual_pan:
            raise ValueError("Individual PAN missing. Capture it during signup before fetching CIBIL.")

        # Restriction: CIBIL can be fetched once per month per borrower.
        await uow.borrowers.consume_cibil_fetch_quota(borrower.id)

        async def audit_cb(**kwargs):
            await uow.api_logs.log_call(application_id=application_id, **kwargs)

        resp = await CibilClient(audit_callback=audit_cb).fetch_report(
            mobile=borrower.mobile, pan=borrower.individual_pan,
            name=borrower.name, gender=borrower.gender or "male",
            application_id=application_id,
        )

        if not resp.success:
            await uow.applications.set_status(
                application_id, "FAILED",
                failure_reason=f"CIBIL API failed: {resp.error}",
            )
            raise RuntimeError(f"CIBIL API failed: {resp.error}")

        cibil = parse_cibil_payload(resp.data)

        # ── Hard Stop B ───────────────────────────────────────────────────────
        if cibil["score"] < CIBIL_HARD_STOP_THRESHOLD:
            hs = _hard_stop_b(cibil["score"], cibil["overdue_amount"])
            await uow.applications.update(
                application_id,
                status           = "FAILED",
                failure_reason   = hs.reason,
                hard_stop_code   = "HARD_STOP_B",
                hard_stop_detail = {
                    "score":          cibil["score"],
                    "threshold":      CIBIL_HARD_STOP_THRESHOLD,
                    "overdue_amount": cibil["overdue_amount"],
                },
                cibil_summary = cibil,
            )
            await uow.audit_logs.log_event(
                application_id=application_id, event="HARD_STOP_B_TRIGGERED",
                old_status=app.status, new_status="FAILED",
                metadata={"score": cibil["score"]},
            )
            return CibilFetchResponse(
                application_id = application_id,
                status         = "FAILED",
                cibil          = _make_cibil_breakdown(cibil),
                result_message = CibilResultMessage(
                    variant  = "hard_stop",
                    headline = f"Your CIBIL score of {cibil['score']} is below our minimum.",
                    detail   = hs.reason,
                ),
                hard_stop  = hs,
                message    = hs.reason,
                next_step  = "Start a fresh analysis once your score improves.",
            )

        # ── Success path ──────────────────────────────────────────────────────
        await uow.applications.store_cibil(application_id, "surepass_cibil", cibil)
        await uow.applications.set_status(application_id, "CIBIL_FETCHED")
        await uow.audit_logs.log_event(
            application_id=application_id, event="CIBIL_FETCHED",
            old_status=app.status, new_status="CIBIL_FETCHED",
            metadata={"score": cibil["score"]},
        )

        result_msg = _cibil_result_message(cibil)
        return CibilFetchResponse(
            application_id = application_id,
            status         = "CIBIL_FETCHED",
            cibil          = _make_cibil_breakdown(cibil),
            result_message = result_msg,
            hard_stop      = None,
            message        = result_msg.detail,
            next_step      = (
                f"POST /api/v1/loan/applications/{application_id}/consent/aa  "
                "{ \"consent\": \"Y\", \"ip_address\": \"<ip>\" }"
            ),
        )

    # =========================================================================
    # Step 5 — Record AA Consent
    # =========================================================================

    async def record_aa_consent(
        self,
        application_id: str,
        consent: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ConsentResponse:
        uow = self._uow
        app = await self._get_app(application_id)
        _guard(app.status, "AA_CONSENT_GIVEN", "record_aa_consent")

        now = datetime.now(timezone.utc)
        await uow.borrowers.update(
            app.borrower_id,
            aa_consent=consent, aa_consent_at=now,
        )
        await uow.applications.set_status(application_id, "AA_CONSENT_GIVEN")
        await uow.audit_logs.log_event(
            application_id=application_id, event="AA_CONSENT_RECORDED",
            old_status=app.status, new_status="AA_CONSENT_GIVEN",
            metadata={"consent": consent, "ip": ip_address},
        )

        if consent == "N":
            return ConsentResponse(
                application_id=application_id, consent_type="AA",
                consent_given=False, recorded_at=now.isoformat(),
                status="AA_CONSENT_GIVEN",
                message="Account Aggregator consent withheld.",
                next_step="Re-submit with consent='Y' to continue.",
            )
        return ConsentResponse(
            application_id=application_id, consent_type="AA",
            consent_given=True, recorded_at=now.isoformat(),
            status="AA_CONSENT_GIVEN",
            message="Account Aggregator consent recorded.",
            next_step=f"POST /api/v1/loan/applications/{application_id}/aa/init",
        )

    # =========================================================================
    # Step 6 — Init AA Session
    # =========================================================================

    async def init_aa(
        self,
        application_id: str,
        bank_mobile: str,
    ) -> AAInitResponse:
        uow      = self._uow
        app      = await self._get_app(application_id)
        _guard(app.status, "AA_INIT_DONE", "init_aa")
        borrower = await uow.borrowers.get_by_id(app.borrower_id)

        if (borrower.aa_consent or "N") != "Y":
            raise ValueError("AA consent not given. Call /consent/aa first.")

        # Store the bank mobile (may differ from personal mobile)
        await uow.borrowers.update(app.borrower_id, aa_bank_mobile=bank_mobile)

        async def audit_cb(**kwargs):
            await uow.api_logs.log_call(application_id=application_id, **kwargs)

        aa_client = AccountAggregatorClient(audit_callback=audit_cb)
        init_resp = await aa_client.init_session(
            mobile_number=bank_mobile,
            pan_number=borrower.pan,
            application_id=application_id,
        )

        if not init_resp.success:
            await uow.applications.set_status(
                application_id, "FAILED",
                failure_reason=f"AA init failed: {init_resp.error}",
            )
            raise RuntimeError(f"AA init failed: {init_resp.error}")

        aa_client_id = AccountAggregatorClient.extract_client_id(init_resp)
        if not aa_client_id:
            raise RuntimeError("AA init did not return a client_id")

        await uow.applications.update(
            application_id, aa_client_id=aa_client_id, status="AA_INIT_DONE",
        )
        await uow.audit_logs.log_event(
            application_id=application_id, event="AA_SESSION_INITIATED",
            old_status=app.status, new_status="AA_INIT_DONE",
            metadata={"aa_client_id": aa_client_id, "bank_mobile_last4": bank_mobile[-4:]},
        )

        redirect_url = (
            (init_resp.data or {}).get("redirect_url")
            or (init_resp.data or {}).get("data", {}).get("redirect_url")
        )

        return AAInitResponse(
            application_id=application_id, aa_client_id=aa_client_id,
            status="AA_INIT_DONE", redirect_url=redirect_url,
            message=(
                f"AA session started. OTP sent to {bank_mobile[:3]}XX X{bank_mobile[-4:]}. "
                "Borrower must complete consent in their banking app."
            ),
            next_step=(
                f"POST /api/v1/loan/applications/{application_id}/aa/complete  "
                '{ "completed": true }  (after borrower completes bank consent)'
            ),
        )

    # =========================================================================
    # Step 7 — Fetch Bank Statement (no polling)
    # =========================================================================

    async def complete_aa_signin(
        self,
        application_id: str,
        *,
        completed: bool = True,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> AACompleteResponse:
        uow = self._uow
        app = await self._get_app(application_id)
        _guard(app.status, "AA_CONSENT_COMPLETED", "complete_aa_signin")

        if not app.aa_client_id:
            raise ValueError("No aa_client_id stored. Call /aa/init first.")
        if not completed:
            raise ValueError(
                "AA sign-in/consent not completed. Submit completed=true after borrower finishes consent."
            )

        await uow.applications.set_status(application_id, "AA_CONSENT_COMPLETED")
        await uow.audit_logs.log_event(
            application_id=application_id,
            event="AA_SIGNIN_CONFIRMED",
            old_status=app.status,
            new_status="AA_CONSENT_COMPLETED",
            metadata={"ip": ip_address, "user_agent": user_agent},
        )

        return AACompleteResponse(
            application_id=application_id,
            status="AA_CONSENT_COMPLETED",
            message="AA sign-in/consent confirmed. Ready to fetch bank statement.",
            next_step=f"POST /api/v1/loan/applications/{application_id}/aa/fetch",
        )

    async def fetch_aa(self, application_id: str) -> AAFetchResponse:
        uow = self._uow
        app = await self._get_app(application_id)
        _guard(app.status, "AA_FETCHED", "fetch_aa")

        if not app.aa_client_id:
            raise ValueError("No aa_client_id stored. Call /aa/init first.")

        async def audit_cb(**kwargs):
            await uow.api_logs.log_call(application_id=application_id, **kwargs)

        aa_resp = await AccountAggregatorClient(audit_callback=audit_cb).fetch_report(
            client_id=app.aa_client_id, application_id=application_id,
        )

        if not aa_resp.success:
            await uow.applications.set_status(
                application_id, "FAILED",
                failure_reason=f"AA fetch failed: {aa_resp.error}",
            )
            raise RuntimeError(f"AA fetch failed: {aa_resp.error}")

        resp_data = aa_resp.data or {}
        aa_status = (
            (resp_data.get("status") or "")
            or (resp_data.get("data", {}).get("status") or "")
        ).upper()
        if aa_status in {"PENDING", "IN_PROGRESS", "INITIATED"}:
            raise ValueError(
                "AA data is not ready yet (provider status=PENDING). "
                "Ensure the borrower completes AA sign-in/consent, then call /aa/complete and retry /aa/fetch."
            )
        if aa_status == "FAILED":
            await uow.applications.set_status(
                application_id,
                "FAILED",
                failure_reason=f"AA session failed: {resp_data}",
            )
            raise RuntimeError(f"AA session failed: {resp_data}")

        aa_data      = parse_aa_payload(aa_resp.data)
        transactions = aa_data["transactions"]
        accounts     = aa_data.get("accounts") or []
        months_set   = {t["transaction_date"][:7] for t in transactions}
        dates        = [t["transaction_date"] for t in transactions]
        total_credits = sum(t["amount"] for t in transactions if t["type"] == "CREDIT")
        active_days   = count_active_days(transactions)

        # Detect historical EMI/OD patterns (triggers Mizan conditional question)
        emi_txns_raw = [
            t for t in transactions
            if t["type"] == "DEBIT" and _looks_like_emi(t["narration"])
        ]
        has_historical_emi = len(emi_txns_raw) > 0
        historical_emi_amt = sum(t["amount"] for t in emi_txns_raw[:3]) / max(len(emi_txns_raw[:3]), 1) if has_historical_emi else 0.0

        # ── EMI Paid/Unpaid Evaluation ────────────────────────────────────────
        # Set emi_paid_unpaid = true ONLY IF:
        # Borrower has successfully paid EMIs for the last N months (N=2), AND
        # There is NO EMI debit transaction in: Current month, OR Previous month
        N_MONTHS_EMI_PAID = 2
        emi_months_set = {t["transaction_date"][:7] for t in emi_txns_raw}
        
        emi_paid_unpaid = False
        if dates and len(emi_months_set) >= N_MONTHS_EMI_PAID:
            all_months_sorted = sorted(list(months_set))
            current_month = all_months_sorted[-1]
            previous_month = all_months_sorted[-2] if len(all_months_sorted) > 1 else None
            
            no_emi_current = current_month not in emi_months_set
            no_emi_previous = previous_month is not None and previous_month not in emi_months_set
            
            if no_emi_current or no_emi_previous:
                emi_paid_unpaid = True

        await uow.applications.update(
            application_id,
            bank_metrics={
                "_raw_transactions": transactions,
                "aa_account_summary": aa_data.get("summary") or {},
                "aa_accounts": accounts,
                "statement_months": len(months_set),
            },
            status="AA_FETCHED",
        )
        await uow.audit_logs.log_event(
            application_id=application_id, event="AA_DATA_FETCHED",
            old_status=app.status, new_status="AA_FETCHED",
            metadata={
                "transaction_count": len(transactions),
                "months":            len(months_set),
                "has_historical_emi": has_historical_emi,
            },
        )

        next_step = (
            f"POST /api/v1/loan/applications/{application_id}/aa/confirm-emi-od"
            if has_historical_emi
            else f"POST /api/v1/loan/applications/{application_id}/process"
        )

        return AAFetchResponse(
            application_id=application_id, status="AA_FETCHED",
            bank_summary=BankStatementSummary(
                transaction_count   = len(transactions),
                months_of_data      = len(months_set),
                date_range_from     = min(dates) if dates else None,
                date_range_to       = max(dates) if dates else None,
                total_credit_inflow = round(total_credits, 2),
                active_credit_days  = active_days,
                has_historical_emi  = has_historical_emi,
                historical_emi_amt  = round(historical_emi_amt, 2),
                emi_paid_unpaid     = emi_paid_unpaid,
            ),
            emi_confirmation_required = has_historical_emi,
            emi_paid_unpaid           = emi_paid_unpaid,
            message=(
                f"Bank statement fetched: {len(transactions)} transactions "
                f"across {len(months_set)} months."
                + (
                    " Historical EMI/OD patterns detected — confirmation required."
                    if has_historical_emi else ""
                )
            ),
            next_step=next_step,
        )

    # =========================================================================
    # Step 7b — Confirm EMI/OD (conditional — Mizan Phase 04)
    # =========================================================================

    async def confirm_emi_od(
        self,
        application_id: str,
        settled: bool,
    ) -> EMIODConfirmResponse:
        uow = self._uow
        app = await self._get_app(application_id)

        if app.status != "AA_FETCHED":
            raise ValueError(
                f"EMI/OD confirmation only valid when status is AA_FETCHED. "
                f"Current: {app.status}."
            )

        await uow.applications.update(
            application_id, emi_od_settled=settled,
        )
        await uow.audit_logs.log_event(
            application_id=application_id, event="EMI_OD_CONFIRMATION_RECORDED",
            metadata={"settled": settled},
        )

        return EMIODConfirmResponse(
            application_id=application_id,
            settled=settled,
            status="AA_FETCHED",
            message=(
                "Understood. We've noted that the EMI/OD obligations are fully settled."
                if settled
                else "Understood. Active EMI/OD obligations will be factored into your borrowing limit."
            ),
            next_step=f"POST /api/v1/loan/applications/{application_id}/process",
        )

    # =========================================================================
    # Step 8 — Process: Safe Borrowing Limit + Lender Matching
    # =========================================================================

    async def process_application(self, application_id: str) -> ProcessApplicationResponse:
        t0  = time.perf_counter()
        uow = self._uow
        app = await self._get_app(application_id)
        _guard(app.status, "PROCESSING", "process_application")

        borrower = await uow.borrowers.get_by_id(app.borrower_id)
        cibil        = app.cibil_summary
        bank_metrics = app.bank_metrics or {}
        raw_txns     = bank_metrics.get("_raw_transactions", [])

        if not cibil:
            raise ValueError("CIBIL data missing. Complete Steps 3–4 first.")
        if not raw_txns:
            raise ValueError("Bank statement missing. Complete Steps 5–7 first.")

        # Restriction: Loan Engine can be used up to 3 times per month per borrower.
        await uow.borrowers.consume_engine_run_quota(borrower.id)

        await uow.applications.set_status(application_id, "PROCESSING")

        async def audit_cb(**kwargs):
            await uow.api_logs.log_call(application_id=application_id, **kwargs)

        # ── Claude Call 1 — transaction classification ─────────────────────
        classifications = await classify_transactions(
            raw_txns, borrower_name=borrower.name,
            application_id=application_id, audit_callback=audit_cb,
        )
        cls_index = build_classification_index(classifications)

        await uow.transaction_labels.bulk_create(application_id, [
            {
                "transaction_id":    t["transaction_id"],
                "amount":            t["amount"],
                "narration":         t["narration"],
                "txn_type":          t["type"],
                "credit_category":   cls_index.get(t["transaction_id"], {}).get("credit_category"),
                "is_emi_obligation": cls_index.get(t["transaction_id"], {}).get("is_emi_obligation"),
                "emi_lender":        cls_index.get(t["transaction_id"], {}).get("emi_lender"),
            }
            for t in raw_txns
        ])

        # ── Engine ────────────────────────────────────────────────────────────
        credit_cls      = [v for v in cls_index.values() if v.get("credit_category")]
        monthly_credits = aggregate_monthly_credits(raw_txns, credit_cls)
        daily_balances  = compute_daily_balances(raw_txns)
        active_days     = count_active_days(raw_txns)

        # ── Existing EMI (v1.2): Bureau + Bank, deduplicated ─────────────────
        # Bank statement analysis detects recurring debits (Claude labels).
        # CIBIL gives EMI obligations from active tradelines.
        #
        # v1.2 requires deduplication across sources: lender name match AND
        # EMI difference ≤ 5% → count once (bank amount kept).
        #
        # All AA-detected EMI transactions (for result card visibility)
        all_emi_txns = [
            t for t in raw_txns
            if t["type"] == "DEBIT"
            and cls_index.get(t["transaction_id"], {}).get("is_emi_obligation") is True
        ]

        # Conditional EMI/OD confirmation (Mizan Phase 04 step 7b):
        #   emi_od_settled=True  → borrower confirmed old patterns cleared
        #                          → use only RECENT EMIs (last 3 months)
        #   emi_od_settled=False → historical obligations still active
        #   emi_od_settled=None  → question wasn't triggered (no historical patterns)
        #                          → use all AA-detected EMIs
        if app.emi_od_settled is True:
            # Keep only EMIs from the last 3 months — historical ones are settled
            cutoff_month = sorted(
                {t["transaction_date"][:7] for t in raw_txns}
            )[-3] if raw_txns else "0000-00"
            emi_txns = [
                t for t in all_emi_txns
                if t["transaction_date"][:7] >= cutoff_month
            ]
        else:
            # Include all AA-detected EMIs (historical + current)
            emi_txns = all_emi_txns

        bank_emi_items = bank_emi_items_from_transactions(emi_txns, cls_index)
        bureau_emi_items = bureau_emi_items_from_cibil_accounts(cibil.get("raw_accounts") or [])
        emi_merge = compute_existing_emi(bureau_items=bureau_emi_items, bank_items=bank_emi_items)
        effective_emi = emi_merge.total

        engine = run_engine(
            monthly_credits         = monthly_credits,
            daily_balances          = daily_balances,
            active_days             = active_days,
            existing_emi            = effective_emi,
            business_vintage_months = borrower.business_vintage_months or 0,
        )

        # ── Lender matching ────────────────────────────────────────────────
        aa_summary = bank_metrics.get("aa_account_summary") or {}
        aa_accounts = bank_metrics.get("aa_accounts") or []
        current_accounts = [
            acct for acct in aa_accounts
            if _aa_account_type(acct) == "CA"
        ]
        current_account_count = len(current_accounts) if aa_accounts else None
        account_type = _aa_account_type(aa_summary)
        months_in_statement = len({t["transaction_date"][:7] for t in raw_txns if t.get("transaction_date")}) if raw_txns else 0
        txn_frequency = round(len(raw_txns) / max(months_in_statement, 1), 2) if raw_txns else None
        account_vintage = _bank_account_vintage_months(raw_txns, aa_summary)
        statement_months = bank_metrics.get("statement_months") or len(monthly_credits)
        risk_band = (engine["risk_band"] or "High Risk").replace(" Risk", "")
        unsecured_track_ratio = None
        if app.target_loan_amount and app.target_loan_amount > 0:
            unsecured_track_ratio = (cibil.get("max_unsecured_loan_outstanding") or 0.0) / app.target_loan_amount

        # ── Turnover + derived income (v1.2) ────────────────────────────────
        turnover_annual: Optional[float] = None
        derived_itr_income_annual: Optional[float] = None
        margin_pct = _margin_pct(borrower.business_nature)
        try:
            birth_or_incorp = _to_ddmmyyyy(borrower.date_of_incorporation)
            itr_pan = (borrower.individual_pan or borrower.pan or "").strip()
            if itr_pan and borrower.business_name and birth_or_incorp:
                itr_resp = await ItrTurnoverClient(audit_callback=audit_cb).fetch_turnover(
                    pan=itr_pan,
                    birth_or_incorporated_date=birth_or_incorp,
                    name=borrower.business_name,
                    application_id=application_id,
                )
                itr_summary = parse_itr_turnover_payload(itr_resp.data if itr_resp.success else None)
                turnover_annual = itr_summary.gross_turnover_annual
        except Exception:
            turnover_annual = None

        if turnover_annual is not None and margin_pct is not None:
            derived_itr_income_annual = turnover_annual * margin_pct

        engine["monthly_banking_credit"] = engine["median_monthly_flow"]
        engine["minimum_transaction_frequency_per_month"] = txn_frequency
        engine["minimum_itr_income_annual"] = derived_itr_income_annual

        bto_ratio = banking_turnover_ratio_pct(
            monthly_banking_credit=engine["bto_monthly_avg"],
            annual_turnover=turnover_annual,
        )
        if bto_ratio is not None:
            engine["bto_ratio_pct"] = round(bto_ratio, 2)
        else:
            engine["bto_ratio_pct"] = None

        ctx = LenderContext(
            loan_type                = "unsecured" if app.loan_type == "Unsecured Term Loan" else "secured",
            loan_amount_requested    = app.target_loan_amount or 0.0,
            borrower_age             = borrower.age or 25,
            business_vintage_months  = borrower.business_vintage_months or 0,
            commercial_premises      = borrower.commercial_premises or "Rented",
            residence_premises       = borrower.residence_premises or "Rented",
            residence_stability_months = None,
            office_stability_months  = None,
            pincode                  = borrower.pincode or "000000",
            business_industry        = borrower.business_industry or "",
            business_type            = borrower.business_nature or "",
            audited_financials_available = None,
            cibil_score              = cibil["score"],
            overdue_amount           = cibil["overdue_amount"],
            payment_delayed_days     = cibil["max_days_overdue"],
            emi_bounce_last_6m       = cibil.get("emi_bounce_last_6m"),
            delinquency_last_12m     = cibil.get("delinquency_last_12m"),
            active_unsecured_loans   = cibil.get("active_unsecured_loans"),
            enquiries_last_2m        = cibil.get("enquiries_last_2m"),
            existing_emi_monthly     = effective_emi,
            proposed_emi_monthly     = engine["stress_emi"],
            unsecured_track_emi_count = cibil.get("unsecured_track_emi_count"),
            unsecured_track_loan_ratio = unsecured_track_ratio,
            max_unsecured_loan_outstanding = cibil.get("max_unsecured_loan_outstanding"),
            account_type             = account_type,
            active_current_account_count = current_account_count,
            transaction_frequency_per_month = txn_frequency,
            bank_account_vintage_months = account_vintage,
            statement_period_months  = statement_months,
            abb_daily                = engine["abb_daily"],
            bto_monthly              = engine["bto_monthly_avg"],
            median_monthly_flow      = engine["median_monthly_flow"],
            qoq_percent              = engine["qoq_pct"],
            volatility_cv            = engine["volatility_index"],
            risk_band                = risk_band,
            safe_loan_amount         = engine["safe_loan_amount"],
            itr_income_annual        = derived_itr_income_annual,
            gst_turnover_annual      = turnover_annual,
            gst_compliance_status    = None,
            gst_filing_regularity_months = None,
        )

        lender_results = registry.evaluate_all(ctx)
        eligible_names   = [r.lender_name for r in lender_results if r.eligible]
        ineligible_names = [r.lender_name for r in lender_results if not r.eligible]

        await uow.lender_decisions.bulk_create(
            application_id, [r.to_dict() for r in lender_results],
        )

        # ── Claude Call 2 — insights ───────────────────────────────────────
        insights = await generate_decision_summary(
            borrower_name      = borrower.name,
            cibil_score        = cibil["score"],
            overdue_amount     = cibil["overdue_amount"],
            effective_emi_monthly = effective_emi,
            median_inflow      = engine["median_monthly_flow"],
            volatility_index   = engine["volatility_index"],
            volatility_interp  = engine["volatility_interpretation"],
            qoq_pct            = engine["qoq_pct"],
            stress_emi         = engine["stress_emi"],
            final_safe_emi     = engine["final_safe_emi"],
            safe_loan_amount   = engine["safe_loan_amount"],
            risk_band          = engine["risk_band"],
            eligible_lenders   = eligible_names,
            ineligible_lenders = ineligible_names,
            application_id     = application_id,
            audit_callback     = audit_cb,
        )

        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        clean_engine = {k: v for k, v in engine.items() if k != "_raw_transactions"}

        await uow.applications.store_final_output(
            application_id,
            engine_output      = clean_engine,
            safe_loan_amount   = engine["safe_loan_amount"],
            risk_band          = engine["risk_band"],
            claude_summary     = insights.safe_borrowing_bullets + [insights.lender_match_bullet],
            processing_time_ms = elapsed,
        )
        await uow.audit_logs.log_event(
            application_id=application_id, event="APPLICATION_COMPLETED",
            old_status="PROCESSING", new_status="COMPLETED",
            metadata={
                "safe_loan_amount": engine["safe_loan_amount"],
                "risk_band":        engine["risk_band"],
                "eligible_lenders": eligible_names,
            },
        )

        target_amt = app.target_loan_amount or 0
        engine_metrics = EngineMetrics(
            total_credit_inflow          = engine["total_credit_inflow"],
            active_days                  = engine["active_days"],
            detected_existing_emi        = engine["detected_existing_emi"],
            abb_daily                    = engine["abb_daily"],
            bto_monthly_avg              = engine["bto_monthly_avg"],
            bto_ratio_pct                = engine.get("bto_ratio_pct"),
            monthly_banking_credit       = engine["monthly_banking_credit"],
            minimum_transaction_frequency_per_month = engine.get("minimum_transaction_frequency_per_month"),
            median_monthly_flow          = engine["median_monthly_flow"],
            std_dev                      = engine["std_dev"],
            volatility_index             = engine["volatility_index"],
            volatility_interpretation    = engine["volatility_interpretation"],
            revenue_concentration_pct    = engine["revenue_concentration_pct"],
            concentration_interpretation = engine["concentration_interpretation"],
            qoq_pct                      = engine["qoq_pct"],
            active_days_ratio            = engine["active_days_ratio"],
            active_days_interpretation   = engine["active_days_interpretation"],
            operating_buffer             = engine["operating_buffer"],
            survival_surplus             = engine["survival_surplus"],
            base_safe_emi                = engine["base_safe_emi"],
            volatility_multiplier        = engine["volatility_multiplier"],
            concentration_multiplier     = engine["concentration_multiplier"],
            vintage_multiplier           = engine["vintage_multiplier"],
            qoq_multiplier               = engine["qoq_multiplier"],
            combined_risk_multiplier     = engine["combined_risk_multiplier"],
            emi_after_penalties          = engine["emi_after_penalties"],
            stress_inflow                = engine["stress_inflow"],
            stress_operating_buffer      = engine["stress_operating_buffer"],
            stress_survival_surplus      = engine["stress_survival_surplus"],
            stress_emi                   = engine["stress_emi"],
            final_safe_emi               = engine["final_safe_emi"],
            risk_band                    = engine["risk_band"],
            tenure_multiplier            = engine["tenure_multiplier"],
            safe_loan_amount             = engine["safe_loan_amount"],
            minimum_itr_income_annual    = engine.get("minimum_itr_income_annual"),
        )

        return ProcessApplicationResponse(
            application_id       = application_id,
            borrower_name        = borrower.name,
            loan_type            = app.loan_type or "",
            target_loan_amount   = target_amt,
            annual_turnover      = turnover_annual,
            existing_emi         = effective_emi,
            status               = "COMPLETED",
            processing_time_ms   = elapsed,
            safe_borrowing_limit = SafeBorrowingLimit(
                safe_loan_amount      = engine["safe_loan_amount"],
                monthly_emi           = engine["final_safe_emi"],
                tenure_months         = engine["tenure_multiplier"],
                risk_band             = engine["risk_band"],
                avg_monthly_inflow    = engine["bto_monthly_avg"],
                existing_emi          = effective_emi,  # bureau + bank, deduped
                annual_turnover       = turnover_annual,
                is_target_achievable  = engine["safe_loan_amount"] >= target_amt,
                engine_metrics        = engine_metrics,
                claude_insights       = insights.safe_borrowing_bullets,
                detected_emi_transactions=[
                    EMITransaction(
                        transaction_id = t["transaction_id"],
                        amount         = t["amount"],
                        narration      = t["narration"],
                        emi_lender     = cls_index.get(t["transaction_id"], {}).get("emi_lender"),
                    )
                    for t in emi_txns
                ],
            ),
            original_request=(
                None if not target_amt or target_amt <= 0 else _original_request_impact(
                    requested_loan_amount=target_amt,
                    tenure_months=engine["tenure_multiplier"],
                    stress_survival_surplus=engine["stress_survival_surplus"],
                    final_safe_emi=engine["final_safe_emi"],
                )
            ),
            lender_matching = LenderMatchingSummary(
                eligible_lenders     = eligible_names,
                ineligible_lenders   = ineligible_names,
                lender_match_insight = insights.lender_match_bullet,
                results=[
                    LenderMatchResult(
                        lender_name        = r.lender_name,
                        likely_to_approve  = r.eligible,
                        fail_reason        = r.fail_reason,
                        all_fail_reasons   = r.all_fail_reasons,
                        pass_count         = r.pass_count,
                        fail_count         = r.fail_count,
                        rule_details=[
                            LenderRuleDetail(
                                rule=rd.rule_name, passed=rd.passed,
                                reason=rd.reason, value=rd.value, threshold=rd.threshold,
                                stage=rd.stage, skipped=rd.skipped,
                            )
                            for rd in r.rule_details
                        ],
                    )
                    for r in lender_results
                ],
            ),
        )

    # =========================================================================
    # Private helpers
    # =========================================================================

    async def _get_app(self, application_id: str):
        app = await self._uow.applications.get_by_id(application_id)
        if not app:
            raise ValueError(f"Application '{application_id}' not found.")
        if app.status == "FAILED":
            hard_stop = app.hard_stop_code or "unknown"
            raise ValueError(
                f"Application is FAILED (hard_stop={hard_stop}): {app.failure_reason}. "
                "Start a fresh application."
            )
        return app

    async def _fetch_business_nature_from_gstin(
        self,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
    ) -> Optional[str]:
        gst_resp = await GstVerificationClient().verify_gst(
            gstin=gstin, fetch_filings=fetch_filings, fy=fy
        )
        if not gst_resp.success:
            return None
        return _extract_business_nature_from_gst_payload(gst_resp.data or {})

    async def _fetch_business_name_from_gstin(
        self,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
    ) -> Optional[str]:
        gst_resp = await GstVerificationClient().verify_gst(
            gstin=gstin, fetch_filings=fetch_filings, fy=fy
        )
        if not gst_resp.success:
            return None
        return _extract_business_name_from_gst_payload(gst_resp.data or {})

    async def _fetch_business_profile_from_gstin(
        self,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
    ) -> dict[str, Optional[str]]:
        gst_resp = await GstVerificationClient().verify_gst(
            gstin=gstin, fetch_filings=fetch_filings, fy=fy
        )
        if not gst_resp.success:
            return {"business_nature": None, "business_name": None}
        payload = gst_resp.data or {}
        return {
            "business_nature": _extract_business_nature_from_gst_payload(payload),
            "business_name": _extract_business_name_from_gst_payload(payload),
        }

    async def _resolve_borrower_identity(
        self,
        gstin: str,
        fetch_filings: bool = True,
        fy: str = "2018-19",
    ) -> dict[str, Optional[str]]:
        gst_resp = await GstVerificationClient().verify_gst(
            gstin=gstin, fetch_filings=fetch_filings, fy=fy
        )
        if not gst_resp.success:
            raise RuntimeError(f"GST verification failed: {gst_resp.error}")

        gst_payload = gst_resp.data or {}
        pan = _extract_pan_from_gst_payload(gst_payload)
        business_name = _extract_business_name_from_gst_payload(gst_payload)
        constitution = _extract_constitution_from_gst_payload(gst_payload)
        trade_name = _extract_trade_name_from_gst_payload(gst_payload)
        address = _extract_primary_address_from_gst_payload(gst_payload)
        if not pan:
            raise RuntimeError("GST verification succeeded but PAN was not present in the response.")

        # CIN/DOI must come only from MCA response.
        cin = None
        date_of_incorporation = None
        mca_resp = await McaGstinClient().fetch_company_identity(gstin=gstin)
        print("bkjbcjkc", mca_resp)
        if mca_resp.success:
            mca_payload = mca_resp.data or {}
            cin = _extract_cin_from_mca_payload(mca_payload)
            date_of_incorporation = _extract_incorporation_date_from_mca_payload(mca_payload)
        # Fail fast instead of silently returning null identity fields.
        if not cin or not date_of_incorporation:
            if not mca_resp.success:
                raise RuntimeError(
                    "MCA lookup failed while resolving CIN/date_of_incorporation. "
                    f"status_code={mca_resp.status_code}, error={mca_resp.error}"
                )
            raise RuntimeError(
                "MCA lookup succeeded but did not return complete CIN/date_of_incorporation."
            )

        return {
            "pan": pan,
            "business_name": business_name,
            "constitution": constitution,
            "trade_name": trade_name,
            "address": address,
            "cin": cin,
            "date_of_incorporation": date_of_incorporation,
        }

    async def _ensure_business_vintage_months(
        self,
        borrower: Any,
        *,
        date_of_incorporation: Optional[str],
        explicit_vintage_provided: bool = False,
    ):
        if borrower is None:
            return borrower
        if borrower.business_vintage_months is not None or explicit_vintage_provided:
            return borrower

        derived_vintage = _business_vintage_months(date_of_incorporation)
        if derived_vintage is None:
            return borrower

        await self._uow.borrowers.update(
            borrower.id,
            business_vintage_months=derived_vintage,
        )
        refreshed = await self._uow.borrowers.get_by_id(borrower.id)
        return refreshed or borrower


def _looks_like_emi(narration: str) -> bool:
    """Heuristic to detect historical EMI/OD patterns before Claude classification."""
    n = narration.upper()
    return any(kw in n for kw in ["EMI", "PPR", "NACH", "ECS", "OD ", "OVERDRAFT", "LOAN"])


def _walk_values(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key, value
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_values(item)


def _find_first_string(payload, candidate_keys):
    for key, value in _walk_values(payload):
        if str(key).strip().lower() in candidate_keys and value not in (None, ""):
            return str(value).strip()
    return None


def _extract_pan_from_gst_payload(payload):
    value = _find_first_string(payload, {"pan", "pan_number", "pannumber"})
    if value:
        return value.upper()
    gstin = _find_first_string(payload, {"gstin", "gst_number", "gst_numbering"})
    if gstin and len(gstin) >= 12:
        return gstin[2:12].upper()
    return None


def _extract_business_nature_from_gst_payload(payload):
    raw = _find_first_string(payload, {"constitution", "business_nature", "businessnature"})
    if not raw:
        return None
    return _map_constitution_to_business_nature(raw)


def _extract_constitution_from_gst_payload(payload):
    return _find_first_string(payload, {"constitution"})


def _extract_trade_name_from_gst_payload(payload):
    return _find_first_string(payload, {"tradename", "trade_name"})


def _extract_business_name_from_gst_payload(payload):
    return _find_first_string(
        payload,
        {
            "legalname",
            "legal_name",
            "legalnameofbusiness",
            "business_name",
            "tradename",
            "trade_name",
        },
    )


def _extract_primary_address_from_gst_payload(payload):
    addresses = payload.get("addresses") if isinstance(payload, dict) else None
    if not isinstance(addresses, list) or not addresses:
        return None

    primary = None
    for entry in addresses:
        if isinstance(entry, dict) and str(entry.get("type", "")).strip().upper() == "PRIMARY":
            primary = entry
            break
    if primary is None:
        candidate = addresses[0]
        primary = candidate if isinstance(candidate, dict) else None
    if not isinstance(primary, dict):
        return None

    parts = [
        str(primary.get("building") or "").strip(),
        str(primary.get("buildingName") or "").strip(),
        str(primary.get("floor") or "").strip(),
        str(primary.get("street") or "").strip(),
        str(primary.get("locality") or "").strip(),
        str(primary.get("district") or "").strip(),
        str(primary.get("state") or "").strip(),
        str(primary.get("zip") or "").strip(),
    ]
    cleaned = [p for p in parts if p]
    return ", ".join(cleaned) if cleaned else None


def _map_constitution_to_business_nature(raw_value: str) -> Optional[str]:
    value = str(raw_value).strip().lower()
    if "wholesale" in value and "retail" in value:
        return "Wholesaler & Retailer"
    if "wholesale" in value:
        return "Wholesaler"
    if "retail" in value:
        return "Retailer"
    if "manufactur" in value:
        return "Manufacturer"
    if "service" in value:
        return "Service Provider"
    if "trader" in value or "trading" in value:
        return "Trader"
    return None


def _margin_pct(business_nature: Optional[str]) -> Optional[float]:
    """
    v1.2 margin table (used to derive net income and minimum ITR income).
    Returns a decimal fraction (e.g. 0.15 for 15%).
    """
    if not business_nature:
        return None
    raw = str(business_nature).strip().lower()
    if raw == "retailer":
        return 0.10
    if raw == "wholesaler":
        return 0.06
    if raw in {"wholesaler & retailer", "retailer and wholesaler", "retailer & wholesaler"}:
        return 0.08
    if raw == "manufacturer":
        return 0.15
    if raw == "trader":
        return 0.06
    return None


def _to_ddmmyyyy(date_yyyy_mm_dd: Optional[str]) -> Optional[str]:
    """
    Attestr ITR API expects DD/MM/YYYY, while we store YYYY-MM-DD.
    """
    if not date_yyyy_mm_dd:
        return None
    raw = str(date_yyyy_mm_dd).strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return None


def _original_request_impact(
    *,
    requested_loan_amount: float,
    tenure_months: int,
    stress_survival_surplus: float,
    final_safe_emi: float,
) -> OriginalRequestImpact:
    requested_emi = emi_from_loan_amount(requested_loan_amount, tenure_months=tenure_months)
    remaining = (stress_survival_surplus or 0.0) - (requested_emi or 0.0)
    stress_ratio = (requested_emi / final_safe_emi) if final_safe_emi and final_safe_emi > 0 else None
    level = requested_loan_risk_level(stress_ratio) or "High Risk"
    return OriginalRequestImpact(
        emi_amount=round(requested_emi, 2),
        remaining_monthly_surplus=round(remaining, 2),
        risk_level=level,
    )


def _extract_cin_from_mca_payload(payload):
    value = _find_first_string(
        payload,
        {
            "cin", "cin_number", "company_cin", "reg", "cinnumber",
            "registration_number", "registrationnumber",
            "corporate_identification_number", "corporateidentificationnumber",
            "company_number", "companynumber",
        },
    )
    return value.upper() if value else None


def _extract_incorporation_date_from_mca_payload(payload):
    raw = _find_first_string(
        payload,
        {
            "date_of_incorporation",
            "incorporation_date",
            "company_incorporation_date",
            "incorporateddate",
            "doi",
            "dateofincorporation",
            "incorporationdate",
        },
    )
    return _normalize_date_string(raw)


def _normalize_date_string(value):
    if not value:
        return None
    raw = str(value).strip()
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return _dt.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10] if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-" else None


def _business_vintage_months(date_of_incorporation: Optional[str]) -> Optional[int]:
    normalized = _normalize_date_string(date_of_incorporation)
    if not normalized:
        return None
    try:
        start = datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now(timezone.utc).date()
    return max(((today.year - start.year) * 12) + (today.month - start.month), 0)


def _aa_account_type(account_like: dict) -> Optional[str]:
    summary = account_like.get("summary_details") or account_like.get("summary") or account_like
    raw = (
        summary.get("accountSubType")
        or summary.get("account_sub_type")
        or summary.get("account_type")
        or summary.get("accountType")
        or ""
    )
    value = str(raw).strip().upper()
    if value in {"CURRENT", "CA"}:
        return "CA"
    if value in {"SAVINGS", "SB"}:
        return "SB"
    return None


def _bank_account_vintage_months(transactions: list[dict], summary: Optional[dict] = None) -> Optional[int]:
    opening_date = str((summary or {}).get("opening_date") or "").strip()
    if opening_date:
        try:
            first = datetime.strptime(opening_date[:10], "%Y-%m-%d")
            last_source = max((t["transaction_date"] for t in transactions if t.get("transaction_date")), default=opening_date[:10])
            last = datetime.strptime(last_source[:10], "%Y-%m-%d")
            return max(((last.year - first.year) * 12) + (last.month - first.month) + 1, 1)
        except ValueError:
            pass
    if not transactions:
        return None
    dates = sorted(t["transaction_date"] for t in transactions if t.get("transaction_date"))
    if not dates:
        return None
    first = datetime.strptime(dates[0], "%Y-%m-%d")
    last = datetime.strptime(dates[-1], "%Y-%m-%d")
    return max(((last.year - first.year) * 12) + (last.month - first.month) + 1, 1)


def _make_cibil_breakdown(cibil: dict) -> CibilScoreBreakdown:
    return CibilScoreBreakdown(
        score                = cibil["score"],
        score_interpretation = _score_interpretation(cibil["score"]),
        overdue_amount       = cibil["overdue_amount"],
        max_days_overdue     = cibil["max_days_overdue"],
        has_written_off      = cibil["has_written_off"],
        active_loan_count    = cibil["active_loan_count"],
        total_emi_from_cibil = cibil["total_emi_from_cibil"],
        recent_enquiries_90d = cibil["recent_enquiries_90d"],
        emi_bounce_last_6m   = cibil.get("emi_bounce_last_6m"),
        emi_bounce_last_12m  = cibil.get("emi_bounce_last_12m", cibil.get("bounce_count_12m")),
        enquiry_count_6m     = cibil.get("enquiry_count_6m"),
        max_unsecured_loan_outstanding = cibil.get("max_unsecured_loan_outstanding"),
    )


def _score_interpretation(score: int) -> str:
    if score >= 750: return "Excellent"
    if score >= 700: return "Good"
    if score >= 650: return "Fair"
    return                 "Poor"
