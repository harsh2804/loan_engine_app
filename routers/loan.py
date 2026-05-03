"""
routers/loan.py
────────────────
Capaxis / Mizan Phase 1 API endpoints.

Mizan Flow → Endpoint Map:
  Phase 01-02  POST /api/v1/borrowers/register              Step 1
  Phase 01-02  POST /api/v1/loan/applications/start         Step 2
  Phase 03     POST /{id}/consent/cibil                     Step 3
  Phase 03     POST /{id}/cibil/fetch                       Step 4  (Hard Stop B here)
  Phase 04     POST /{id}/consent/aa                        Step 5
  Phase 04     POST /{id}/aa/init                           Step 6
  Phase 04     POST /{id}/aa/fetch                          Step 7
  Phase 04     POST /{id}/aa/confirm-emi-od                 Step 7b (conditional)
  Phase 05     POST /{id}/process                           Step 8

Queries:
  GET /api/v1/loan/applications/{id}
  GET /api/v1/loan/applications/{id}/audit
  GET /api/v1/loan/applications/{id}/api-logs
  GET /api/v1/borrowers/{pan}
  GET /api/v1/borrowers/{pan}/applications
  GET /api/v1/lenders
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.repositories.repositories import UnitOfWork
from models.schemas import (
    GstinVerifyRequest, GstinVerifyResponse, SignupPage1Request,
    BorrowerRegisterRequest, BorrowerRegisterResponse,
    ApplicationStartRequest, ApplicationStartResponse,
    ConsentRequest, ConsentResponse,
    CibilFetchResponse,
    AAInitRequest, AAInitResponse,
    AACompleteRequest, AACompleteResponse,
    AAFetchResponse,
    EMIODConfirmRequest, EMIODConfirmResponse,
    ProcessApplicationResponse,
    ApplicationStatusResponse,
    AuditLogSchema,
    BorrowerProfileResponse,
)
from services.loan_orchestrator import LoanOrchestrator

router = APIRouter(prefix="/api/v1", tags=["Capaxis Mizan Engine"])


# ── Dependencies ───────────────────────────────────────────────────────────────

async def get_uow(db: AsyncSession = Depends(get_db_session)) -> UnitOfWork:
    return UnitOfWork(db)


def get_orchestrator(uow: UnitOfWork = Depends(get_uow)) -> LoanOrchestrator:
    return LoanOrchestrator(uow)


def _err(exc: Exception) -> HTTPException:
    """ValueError → 422 | RuntimeError → 502 | everything else → 500."""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


# =============================================================================
# Step 1 — Register Borrower (Mizan Phase 01-02)
# =============================================================================

@router.post(
    "/signup/verify-gstin",
    response_model=GstinVerifyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 0 — Verify GSTIN and store identity in signup table",
    description=(
        "Verifies GSTIN and stores/updates GSTIN, company PAN, CIN and date of incorporation in the signup table.\n\n"
        "Returns `signup_id` to continue the borrower signup flow."
    ),
)
async def verify_gstin(
    request: GstinVerifyRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.verify_gstin_for_signup(
            request.gstin,
            fetch_filings=request.fetch_filings,
            fy=request.fy,
        )
    except Exception as exc:
        raise _err(exc)


@router.post(
    "/signup/page-1",
    response_model=BorrowerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 0b — Signup Page 1 (create/link borrower)",
    description=(
        "Captures borrower personal details (name, mobile, DOB, gender, individual PAN for CIBIL) and links a borrower record to the verified signup row.\n\n"
        "Identity fields (GSTIN/PAN/CIN/DOI) are always sourced from the signup table."
    ),
)
async def signup_page_1(
    request: SignupPage1Request,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.complete_signup_page_1(request)
    except Exception as exc:
        raise _err(exc)


@router.post(
    "/borrowers/register",
    response_model=BorrowerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 1 — Register or update borrower profile",
    description=(
        "Collects borrower PII and all Mizan Phase 02 gate answers.\n\n"
        "`gstin`, PAN, CIN and date of incorporation are sourced from the signup table "
        "(preferred: send `borrower_id`; legacy fallback: send `gstin`).\n\n"
        "**Saved-once fields** (only collected on first visit, stored permanently):\n"
        "business_nature, business_industry, business_product, commercial_premises, "
        "residence_premises, whatsapp_number, has_current_account.\n\n"
        "**Hard Stop A**: if `has_current_account=false`, the response contains "
        "a `hard_stop` object with guidance and the conversation ends.\n\n"
        "For returning borrowers, omit saved-once fields — stored values are kept."
    ),
)
async def register_borrower(
    request: BorrowerRegisterRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.register_borrower(request)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 2 — Start Application (Mizan Phase 01: loan type + Gate 1: target amount)
# =============================================================================

@router.post(
    "/loan/applications/start",
    response_model=ApplicationStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 2 — Start loan application",
    description=(
        "Creates a new loan application.\n\n"
        "`loan_type`: Unsecured Term Loan | Secured Term Loan\n"
        "`target_loan_amount`: ₹ amount the borrower wants to borrow (Gate 1)\n\n"
        "Will be rejected if borrower profile is incomplete or `has_current_account=false`."
    ),
)
async def start_application(
    request: ApplicationStartRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        individual_pan = request.individual_pan #or request.borrower_pan
        if not individual_pan:
            raise ValueError("individual_pan is required (borrower_pan is deprecated).")
        return await orc.start_application(
            individual_pan     = individual_pan,
            loan_type          = request.loan_type,
            target_loan_amount = request.target_loan_amount,
        )
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 3 — CIBIL Consent (Mizan Phase 03)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/consent/cibil",
    response_model=ConsentResponse,
    summary="Step 3 — Record borrower CIBIL consent",
    description=(
        "Mizan text: 'This is a soft check — it does not affect your credit score.'\n\n"
        "Consent (Y/N) is stored with timestamp and IP address for regulatory compliance. "
        "Must be Y before Step 4 can proceed."
    ),
)
async def cibil_consent(
    application_id: str,
    request: ConsentRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.record_cibil_consent(
            application_id,
            consent    = request.consent,
            ip_address = request.ip_address,
            user_agent = request.user_agent,
        )
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 4 — Fetch CIBIL (Mizan Phase 03 — includes Hard Stop B)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/cibil/fetch",
    response_model=CibilFetchResponse,
    summary="Step 4 — Fetch CIBIL report via Surepass",
    description=(
        "Calls the Surepass CIBIL API using stored PAN + mobile.\n\n"
        "**Hard Stop B**: if score < 650, the response contains `hard_stop` "
        "and status becomes FAILED. The Mizan conversation ends with specific guidance.\n\n"
        "On success (score ≥ 650), returns the CIBIL result card and Mizan's "
        "variant message (clean / issues)."
    ),
)
async def fetch_cibil(
    application_id: str,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.fetch_cibil(application_id)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 5 — AA Consent (Mizan Phase 04)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/consent/aa",
    response_model=ConsentResponse,
    summary="Step 5 — Record Account Aggregator consent",
    description=(
        "Mizan text: 'RBI regulated data-sharing system. Read-only. "
        "I cannot initiate transactions or store raw statements.'\n\n"
        "Consent stored with timestamp and IP."
    ),
)
async def aa_consent(
    application_id: str,
    request: ConsentRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.record_aa_consent(
            application_id,
            consent    = request.consent,
            ip_address = request.ip_address,
            user_agent = request.user_agent,
        )
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 6 — AA Init (Mizan Phase 04 — separate bank mobile)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/aa/init",
    response_model=AAInitResponse,
    summary="Step 6 — Initialise Account Aggregator session",
    description=(
        "Starts the AA session using the mobile number linked to the borrower's "
        "**business current account** (may differ from personal mobile — Mizan Phase 04).\n\n"
        "Returns `aa_client_id` and optional `redirect_url` for the banking app consent screen."
    ),
)
async def init_aa(
    application_id: str,
    request: AAInitRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.init_aa(application_id, bank_mobile=request.bank_mobile)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 7 — Fetch AA (Mizan Phase 04 — no polling)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/aa/complete",
    response_model=AACompleteResponse,
    summary="Step 6b â€” Confirm borrower completed AA sign-in/consent",
    description=(
        "Frontend-driven gating step.\n\n"
        "Call this only after the borrower completes AA sign-in/consent in their banking app. "
        "This prevents the backend from attempting to fetch AA data while the session is still pending."
    ),
)
async def complete_aa(
    application_id: str,
    request: AACompleteRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.complete_aa_signin(
            application_id,
            completed=request.completed,
            ip_address=request.ip_address,
            user_agent=request.user_agent,
        )
    except Exception as exc:
        raise _err(exc)


@router.post(
    "/loan/applications/{application_id}/aa/fetch",
    response_model=AAFetchResponse,
    summary="Step 7 — Fetch bank statement via Account Aggregator",
    description=(
        "Single call — no polling. Call after the borrower completes bank consent "
        "in their banking app.\n\n"
        "If `emi_confirmation_required=true` in the response, call Step 7b before Step 8. "
        "This is the Mizan conditional question: "
        "'I noticed EMI/OD payments — are these fully settled?'"
    ),
)
async def fetch_aa(
    application_id: str,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.fetch_aa(application_id)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 7b — EMI/OD Confirmation (conditional — Mizan Phase 04)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/aa/confirm-emi-od",
    response_model=EMIODConfirmResponse,
    summary="Step 7b — Confirm whether historical EMI/OD obligations are settled",
    description=(
        "Only call this if Step 7 returns `emi_confirmation_required=true`.\n\n"
        "Mizan script: 'I noticed EMI or overdraft payments in your bank statement up until "
        "about 3 months ago. Are these obligations fully settled now?'\n\n"
        "If `settled=true`: historical bank EMIs older than ~3 months are ignored.\n"
        "If `settled=false`: all bank-detected EMIs are considered.\n\n"
        "In both cases, bureau (CIBIL) + bank EMIs are deduplicated per v1.2 rules."
    ),
)
async def confirm_emi_od(
    application_id: str,
    request: EMIODConfirmRequest,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.confirm_emi_od(application_id, settled=request.settled)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Step 8 — Process (Mizan Phase 05)
# =============================================================================

@router.post(
    "/loan/applications/{application_id}/process",
    response_model=ProcessApplicationResponse,
    summary="Step 8 — Compute Safe Borrowing Limit + Lender Matching",
    description=(
        "Runs the full Capaxis Phase 1 engine:\n\n"
        "**Product 1 — Safe Borrowing Limit** (Mizan Phase 05)\n"
        "- Claude classifies all bank transactions\n"
        "- Deterministic engine: BTO → μ → CV → QoQ → penalties → stress test → safe loan\n"
        "- `is_target_achievable`: whether the safe amount ≥ borrower's requested amount\n"
        "- 5 Claude insights for the 'Why This Amount?' screen\n"
        "- Mandatory disclaimer: 'Not a guaranteed approval'\n\n"
        "**Product 2 — Lender Matching**\n"
        "- All registered lenders evaluated with per-rule transparency\n"
        "- 1 Claude sentence for the lender match summary\n"
    ),
)
async def process_application(
    application_id: str,
    orc: LoanOrchestrator = Depends(get_orchestrator),
):
    try:
        return await orc.process_application(application_id)
    except Exception as exc:
        raise _err(exc)


# =============================================================================
# Query Endpoints
# =============================================================================

@router.get(
    "/loan/applications/{application_id}",
    response_model=ApplicationStatusResponse,
    summary="Get application status and pipeline stage",
)
async def get_application_status(
    application_id: str,
    uow: UnitOfWork = Depends(get_uow),
):
    app = await uow.applications.get_by_id(application_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    borrower = await uow.borrowers.get_by_id(app.borrower_id)

    return ApplicationStatusResponse(
        application_id     = app.id,
        loan_type          = app.loan_type,
        target_loan_amount = app.target_loan_amount,
        status             = app.status,
        hard_stop_code     = app.hard_stop_code,
        created_at         = app.created_at.isoformat(),
        updated_at         = app.updated_at.isoformat(),
        cibil_consent      = borrower.cibil_consent if borrower else None,
        aa_consent         = borrower.aa_consent    if borrower else None,
        aa_client_id       = app.aa_client_id,
        safe_loan_amount   = app.safe_loan_amount,
        risk_band          = app.risk_band,
        failure_reason     = app.failure_reason,
    )


@router.get(
    "/loan/applications/{application_id}/audit",
    response_model=list[AuditLogSchema],
    summary="Full audit trail — all events including consent and hard stops",
)
async def get_audit_trail(
    application_id: str,
    uow: UnitOfWork = Depends(get_uow),
):
    logs = await uow.audit_logs.get_application_history(application_id)
    return [
        AuditLogSchema(
            id=log.id, event=log.event,
            old_status=log.old_status, new_status=log.new_status,
            actor=log.actor, created_at=log.created_at.isoformat(),
            metadata=log.extra_metadata,
        )
        for log in logs
    ]


@router.get(
    "/loan/applications/{application_id}/api-logs",
    summary="Outbound API call logs (CIBIL, AA, Claude)",
)
async def get_api_logs(
    application_id: str,
    uow: UnitOfWork = Depends(get_uow),
):
    logs = await uow.api_logs.get_by_application(application_id)
    return [
        {
            "id":          log.id,
            "service":     log.service,
            "endpoint":    log.endpoint,
            "status_code": log.status_code,
            "duration_ms": log.duration_ms,
            "success":     log.success,
            "attempt":     log.attempt_number,
            "error":       log.error_message,
            "created_at":  log.created_at.isoformat(),
        }
        for log in logs
    ]


@router.get(
    "/borrowers/{pan}",
    response_model=BorrowerProfileResponse,
    summary="Get borrower profile — includes saved-once fields and consent status",
)
async def get_borrower(
    pan: str,
    uow: UnitOfWork = Depends(get_uow),
):
    borrower = await uow.borrowers.get_by_pan(pan.upper())
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    return BorrowerProfileResponse(
        borrower_id             = borrower.id,
        name                    = borrower.name,
        gstin                   = borrower.gstin,
        pan                     = borrower.pan,
        individual_pan          = borrower.individual_pan,
        cin                     = borrower.cin,
        mobile                  = borrower.mobile,
        email                   = borrower.email,
        gender                  = borrower.gender,
        age                     = borrower.age,
        date_of_incorporation   = borrower.date_of_incorporation,
        date_of_birth           = borrower.date_of_birth,
        business_name           = borrower.business_name,
        business_nature         = borrower.business_nature,
        business_industry       = borrower.business_industry,
        business_product        = borrower.business_product,
        business_vintage_months = borrower.business_vintage_months,
        commercial_premises     = borrower.commercial_premises,
        residence_premises      = borrower.residence_premises,
        pincode                 = borrower.pincode,
        whatsapp_number         = borrower.whatsapp_number,
        has_current_account     = borrower.has_current_account,
        aa_bank_mobile          = borrower.aa_bank_mobile,
        profile_complete        = borrower.profile_complete,
        missing_fields          = borrower.missing_profile_fields,
        cibil_consent           = borrower.cibil_consent,
        cibil_consent_at        = borrower.cibil_consent_at.isoformat() if borrower.cibil_consent_at else None,
        aa_consent              = borrower.aa_consent,
        aa_consent_at           = borrower.aa_consent_at.isoformat() if borrower.aa_consent_at else None,
        created_at              = borrower.created_at.isoformat(),
    )


@router.get(
    "/borrowers/{pan}/applications",
    response_model=list[ApplicationStatusResponse],
    summary="All applications for a borrower",
)
async def get_borrower_applications(
    pan: str,
    uow: UnitOfWork = Depends(get_uow),
):
    borrower = await uow.borrowers.get_by_pan(pan.upper())
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    apps = await uow.applications.get_by_borrower(borrower.id)
    return [
        ApplicationStatusResponse(
            application_id     = a.id,
            loan_type          = a.loan_type,
            target_loan_amount = a.target_loan_amount,
            status             = a.status,
            hard_stop_code     = a.hard_stop_code,
            created_at         = a.created_at.isoformat(),
            updated_at         = a.updated_at.isoformat(),
            cibil_consent      = borrower.cibil_consent,
            aa_consent         = borrower.aa_consent,
            aa_client_id       = a.aa_client_id,
            safe_loan_amount   = a.safe_loan_amount,
            risk_band          = a.risk_band,
            failure_reason     = a.failure_reason,
        )
        for a in apps
    ]


@router.get("/lenders", summary="List all registered lenders and their count")
async def list_lenders():
    from lenders.registry import registry
    lenders = registry.list_lenders()
    return {"lenders": lenders, "total_count": len(lenders)}
