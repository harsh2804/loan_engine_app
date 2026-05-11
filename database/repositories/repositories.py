"""
database/repositories/repositories.py
──────────────────────────────────────
Domain-specific repositories built on top of BaseRepository.
Each adds query methods specific to that model.

Exposes a single factory function `make_repos(session)` for DI.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    Borrower, Signup, LoanApplication, ApiCallLog,
    AuditLog, LenderDecision, TransactionLabel,
)
from database.repositories.base import BaseRepository
from utils.usage_limits import MonthlyQuotaState, consume_monthly_quota


async def _upsert_signup_row(
    session: AsyncSession,
    *,
    borrower_id: str,
    gstin: str,
    pan: str,
    business_name: Optional[str],
    constitution: Optional[str],
    trade_name: Optional[str],
    address: Optional[str],
    cin: Optional[str],
    date_of_incorporation: Optional[str],
) -> Signup:
    stmt = select(Signup).where(
        Signup.borrower_id == borrower_id,
        Signup.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    signup = result.scalar_one_or_none()
    if signup:
        signup.gstin = gstin
        signup.pan = pan
        signup.business_name = business_name
        signup.constitution = constitution
        signup.trade_name = trade_name
        signup.address = address
        signup.cin = cin
        signup.date_of_incorporation = date_of_incorporation
    else:
        signup = Signup(
            borrower_id=borrower_id,
            gstin=gstin,
            pan=pan,
            business_name=business_name,
            constitution=constitution,
            trade_name=trade_name,
            address=address,
            cin=cin,
            date_of_incorporation=date_of_incorporation,
        )
        session.add(signup)
    await session.flush()
    return signup


# ── Borrower ──────────────────────────────────────────────────────────────────

class BorrowerRepository(BaseRepository[Borrower]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Borrower, session)

    async def get_by_id(self, record_id: str) -> Optional[Borrower]:
        stmt = select(Borrower).options(selectinload(Borrower.signup)).where(
            Borrower.id == record_id,
            Borrower.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_pan(self, pan: str) -> Optional[Borrower]:
        stmt = (
            select(Borrower)
            .join(Borrower.signup)
            .options(selectinload(Borrower.signup))
            .where(
                Signup.pan == pan.upper(),
                Borrower.deleted_at.is_(None),
                Signup.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_individual_pan(self, individual_pan: str) -> Optional[Borrower]:
        stmt = (
            select(Borrower)
            .join(Borrower.signup)
            .options(selectinload(Borrower.signup))
            .where(
                Borrower.individual_pan == individual_pan.upper(),
                Borrower.deleted_at.is_(None),
                Signup.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_gstin(self, gstin: str) -> Optional[Borrower]:
        stmt = (
            select(Borrower)
            .join(Borrower.signup)
            .options(selectinload(Borrower.signup))
            .where(
                Signup.gstin == gstin.upper(),
                Borrower.deleted_at.is_(None),
                Signup.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_mobile(self, mobile: str) -> Optional[Borrower]:
        stmt = select(Borrower).options(selectinload(Borrower.signup)).where(
            Borrower.mobile == mobile,
            Borrower.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(self, pan: str, **kwargs: object) -> Borrower:
        """
        Insert borrower if PAN doesn't exist, otherwise update profile fields.
        Returns the borrower record.
        """
        existing = await self.get_by_pan(pan)
        if existing:
            borrower_updates = {k: v for k, v in kwargs.items() if v is not None and k not in {"gstin", "pan", "cin", "date_of_incorporation"}}
            if borrower_updates:
                await self.update(existing.id, **borrower_updates)
            await _upsert_signup_row(
                self._session,
                borrower_id=existing.id,
                gstin=str(kwargs.get("gstin", existing.gstin)).upper(),
                pan=pan.upper(),
                business_name=kwargs.get("business_name"),
                constitution=kwargs.get("constitution"),
                trade_name=kwargs.get("trade_name"),
                address=kwargs.get("address"),
                cin=kwargs.get("cin") if kwargs.get("cin") is not None else existing.cin,
                date_of_incorporation=kwargs.get("date_of_incorporation") if kwargs.get("date_of_incorporation") is not None else existing.date_of_incorporation,
            )
            return await self.get_by_id(existing.id)  # type: ignore[return-value]

        if kwargs.get("gstin") is None:
            raise ValueError("gstin is required when creating a borrower via upsert(pan=...)")

        borrower_updates = {k: v for k, v in kwargs.items() if v is not None and k not in {"gstin", "pan", "cin", "date_of_incorporation"}}
        borrower = await self.create(**borrower_updates)
        await _upsert_signup_row(
            self._session,
            borrower_id=borrower.id,
            gstin=str(kwargs["gstin"]).upper(),
            pan=pan.upper(),
            business_name=kwargs.get("business_name"),
            constitution=kwargs.get("constitution"),
            trade_name=kwargs.get("trade_name"),
            address=kwargs.get("address"),
            cin=kwargs.get("cin"),
            date_of_incorporation=kwargs.get("date_of_incorporation"),
        )
        return await self.get_by_id(borrower.id)  # type: ignore[return-value]

    async def upsert_by_gstin(self, gstin: str, **kwargs: object) -> Borrower:
        existing = await self.get_by_gstin(gstin)
        if existing:
            borrower_updates = {k: v for k, v in kwargs.items() if v is not None and k not in {"gstin", "pan", "cin", "date_of_incorporation"}}
            if borrower_updates:
                await self.update(existing.id, **borrower_updates)
            pan = kwargs.get("pan", existing.pan)
            await _upsert_signup_row(
                self._session,
                borrower_id=existing.id,
                gstin=gstin.upper(),
                pan=str(pan).upper(),
                business_name=kwargs.get("business_name"),
                constitution=kwargs.get("constitution"),
                trade_name=kwargs.get("trade_name"),
                address=kwargs.get("address"),
                cin=kwargs.get("cin") if kwargs.get("cin") is not None else existing.cin,
                date_of_incorporation=kwargs.get("date_of_incorporation") if kwargs.get("date_of_incorporation") is not None else existing.date_of_incorporation,
            )
            return await self.get_by_id(existing.id)  # type: ignore[return-value]

        if kwargs.get("pan") is None:
            raise ValueError("pan is required when creating a borrower via upsert_by_gstin(gstin=...)")

        borrower_updates = {k: v for k, v in kwargs.items() if v is not None and k not in {"gstin", "pan", "cin", "date_of_incorporation"}}
        borrower = await self.create(**borrower_updates)
        await _upsert_signup_row(
            self._session,
            borrower_id=borrower.id,
            gstin=gstin.upper(),
            pan=str(kwargs["pan"]).upper(),
            business_name=kwargs.get("business_name"),
            constitution=kwargs.get("constitution"),
            trade_name=kwargs.get("trade_name"),
            address=kwargs.get("address"),
            cin=kwargs.get("cin"),
            date_of_incorporation=kwargs.get("date_of_incorporation"),
        )
        return await self.get_by_id(borrower.id)  # type: ignore[return-value]

    # ── Monthly usage limits ────────────────────────────────────────────────

    async def consume_engine_run_quota(self, borrower_id: str, *, limit: int = 3) -> None:
        """
        Engine (Step 8) can run up to `limit` times per calendar month.
        Resets on the 1st of every month.
        """
        stmt = select(Borrower).where(
            Borrower.id == borrower_id,
            Borrower.deleted_at.is_(None),
        ).with_for_update()
        result = await self._session.execute(stmt)
        borrower = result.scalar_one_or_none()
        if not borrower:
            raise ValueError("Borrower not found.")

        quota = consume_monthly_quota(
            state=MonthlyQuotaState(month=borrower.engine_runs_month, count=borrower.engine_runs_count),
            limit=limit,
            now=datetime.now(timezone.utc),
        )
        if not quota.allowed:
            raise ValueError(
                f"Loan Engine limit reached: {limit} runs per month. "
                f"Resets on {quota.next_reset_date}. (AA usage is unlimited.)"
            )

        borrower.engine_runs_month = quota.state.month
        borrower.engine_runs_count = quota.state.count
        await self._session.flush()

    async def consume_cibil_fetch_quota(self, borrower_id: str, *, limit: int = 1) -> None:
        """
        CIBIL report (Step 4) can be fetched up to `limit` times per calendar month.
        Resets on the 1st of every month.
        """
        stmt = select(Borrower).where(
            Borrower.id == borrower_id,
            Borrower.deleted_at.is_(None),
        ).with_for_update()
        result = await self._session.execute(stmt)
        borrower = result.scalar_one_or_none()
        if not borrower:
            raise ValueError("Borrower not found.")

        quota = consume_monthly_quota(
            state=MonthlyQuotaState(month=borrower.cibil_fetch_month, count=borrower.cibil_fetch_count),
            limit=limit,
            now=datetime.now(timezone.utc),
        )
        if not quota.allowed:
            raise ValueError(
                f"CIBIL fetch limit reached: {limit} per month. "
                f"Resets on {quota.next_reset_date}."
            )

        borrower.cibil_fetch_month = quota.state.month
        borrower.cibil_fetch_count = quota.state.count
        await self._session.flush()


# ── Loan Application ──────────────────────────────────────────────────────────

# ── Signup ───────────────────────────────────────────────────────────────────

class SignupRepository(BaseRepository[Signup]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Signup, session)

    async def get_by_pan(self, pan: str) -> Optional[Signup]:
        return await self.get_by_field("pan", pan.upper())

    async def get_by_gstin(self, gstin: str) -> Optional[Signup]:
        return await self.get_by_field("gstin", gstin.upper())

    async def get_by_borrower_id(self, borrower_id: str) -> Optional[Signup]:
        return await self.get_by_field("borrower_id", borrower_id)


class ApplicationRepository(BaseRepository[LoanApplication]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(LoanApplication, session)

    async def get_by_borrower(
        self,
        borrower_id: str,
        *,
        limit: int = 20,
    ) -> Sequence[LoanApplication]:
        return await self.list_by_field("borrower_id", borrower_id, limit=limit)

    async def set_status(
        self,
        application_id: str,
        status: str,
        failure_reason: Optional[str] = None,
    ) -> None:
        updates: dict = {"status": status}
        if failure_reason:
            updates["failure_reason"] = failure_reason
        await self.update(application_id, **updates)

    async def store_cibil(self, application_id: str, cibil_client_id: str, summary: dict) -> None:
        await self.update(
            application_id,
            cibil_client_id=cibil_client_id,
            cibil_summary=summary,
        )

    async def store_bank_metrics(self, application_id: str, aa_client_id: str, metrics: dict) -> None:
        await self.update(
            application_id,
            aa_client_id=aa_client_id,
            bank_metrics=metrics,
        )

    async def store_final_output(
        self,
        application_id: str,
        engine_output: dict,
        safe_loan_amount: float,
        risk_band: str,
        claude_summary: list,
        processing_time_ms: float,
    ) -> None:
        await self.update(
            application_id,
            engine_output=engine_output,
            safe_loan_amount=safe_loan_amount,
            risk_band=risk_band,
            claude_summary=claude_summary,
            processing_time_ms=processing_time_ms,
            status="COMPLETED",
        )


# ── API Call Log ──────────────────────────────────────────────────────────────

class ApiCallLogRepository(BaseRepository[ApiCallLog]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(ApiCallLog, session)

    async def log_call(
        self,
        *,
        application_id: str,
        service: str,
        endpoint: str,
        method: str = "POST",
        request_body: Optional[dict]  = None,
        response_body: Optional[dict] = None,
        status_code: Optional[int]    = None,
        duration_ms: Optional[float]  = None,
        success: bool                 = False,
        error_message: Optional[str]  = None,
        attempt_number: int           = 1,
    ) -> ApiCallLog:
        """
        Masks PAN and mobile before writing to DB.
        """
        safe_request = _mask_pii(request_body) if request_body else None
        return await self.create(
            application_id=application_id,
            service=service,
            endpoint=endpoint,
            method=method,
            request_body=safe_request,
            response_body=response_body,
            status_code=status_code,
            duration_ms=duration_ms,
            success=success,
            error_message=error_message,
            attempt_number=attempt_number,
        )

    async def get_by_application(self, application_id: str) -> Sequence[ApiCallLog]:
        return await self.list_by_field("application_id", application_id)


# ── Audit Log ─────────────────────────────────────────────────────────────────

class AuditLogRepository(BaseRepository[AuditLog]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(AuditLog, session)

    async def log_event(
        self,
        *,
        application_id: str,
        event: str,
        old_status: Optional[str]  = None,
        new_status: Optional[str]  = None,
        actor: str                 = "system",
        metadata: Optional[dict]   = None,
    ) -> AuditLog:
        return await self.create(
            application_id=application_id,
            event=event,
            old_status=old_status,
            new_status=new_status,
            actor=actor,
            extra_metadata=metadata,
        )

    async def get_application_history(self, application_id: str) -> Sequence[AuditLog]:
        return await self.list_by_field("application_id", application_id)


# ── Lender Decision ───────────────────────────────────────────────────────────

class LenderDecisionRepository(BaseRepository[LenderDecision]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(LenderDecision, session)

    async def bulk_create(
        self,
        application_id: str,
        decisions: list[dict],
    ) -> None:
        for d in decisions:
            await self.create(
                application_id=application_id,
                lender_name=d["lender_name"],
                eligible=d["eligible"],
                fail_reason=d.get("fail_reason"),
                rule_details=d.get("rule_details"),
            )

    async def get_eligible(self, application_id: str) -> Sequence[LenderDecision]:
        stmt = select(LenderDecision).where(
            and_(
                LenderDecision.application_id == application_id,
                LenderDecision.eligible.is_(True),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()


# ── Transaction Label ─────────────────────────────────────────────────────────

class TransactionLabelRepository(BaseRepository[TransactionLabel]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(TransactionLabel, session)

    async def bulk_create(
        self,
        application_id: str,
        labels: list[dict],
    ) -> None:
        for label in labels:
            await self.create(
                application_id=application_id,
                transaction_id=label["transaction_id"],
                amount=label["amount"],
                narration=label["narration"],
                txn_type=label["txn_type"],
                credit_category=label.get("credit_category"),
                is_emi_obligation=label.get("is_emi_obligation"),
                emi_lender=label.get("emi_lender"),
            )


# ── Factory ───────────────────────────────────────────────────────────────────

class UnitOfWork:
    """
    Groups all repositories that share a session.
    Inject this via FastAPI's Depends(get_unit_of_work).
    """
    def __init__(self, session: AsyncSession) -> None:
        self.session      = session
        self.borrowers    = BorrowerRepository(session)
        self.signups      = SignupRepository(session)
        self.applications = ApplicationRepository(session)
        self.api_logs     = ApiCallLogRepository(session)
        self.audit_logs   = AuditLogRepository(session)
        self.lender_decisions    = LenderDecisionRepository(session)
        self.transaction_labels  = TransactionLabelRepository(session)


# ── PII masking ───────────────────────────────────────────────────────────────

_PII_FIELDS = {"pan", "gstin", "mobile", "mobile_number", "pan_number", "name"}

def _mask_pii(data: dict) -> dict:
    """Replace sensitive fields with masked versions before DB storage."""
    result = {}
    for k, v in data.items():
        if k.lower() in _PII_FIELDS and isinstance(v, str) and len(v) > 4:
            result[k] = v[:2] + "***" + v[-2:]
        else:
            result[k] = v
    return result
