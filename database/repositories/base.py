"""
database/repositories/base.py
──────────────────────────────
Generic async repository — typed CRUD operations.
All domain repositories inherit from this.

Design:
  - Accepts AsyncSession via constructor (injected)
  - No session lifecycle management here (that's the FastAPI dependency's job)
  - Supports soft-delete via deleted_at column
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Generic, Optional, Sequence, Type, TypeVar

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    def __init__(self, model: Type[ModelT], session: AsyncSession) -> None:
        self._model   = model
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, **kwargs: Any) -> ModelT:
        instance = self._model(**kwargs)
        self._session.add(instance)
        await self._session.flush()   # get the ID without committing
        return instance

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, record_id: str) -> Optional[ModelT]:
        stmt = select(self._model).where(
            self._model.id == record_id,
            self._model.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> Sequence[ModelT]:
        stmt = select(self._model)
        if not include_deleted:
            stmt = stmt.where(self._model.deleted_at.is_(None))
        stmt = stmt.limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_by_field(self, field: str, value: Any) -> Optional[ModelT]:
        col = getattr(self._model, field)
        stmt = select(self._model).where(
            col == value,
            self._model.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_field(
        self,
        field: str,
        value: Any,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ModelT]:
        col  = getattr(self._model, field)
        stmt = (
            select(self._model)
            .where(col == value, self._model.deleted_at.is_(None))
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(self, record_id: str, **kwargs: Any) -> Optional[ModelT]:
        stmt = (
            update(self._model)
            .where(self._model.id == record_id)
            .values(**kwargs)
            .returning(self._model)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()

    # ── Soft Delete ──────────────────────────────────────────────────────────

    async def soft_delete(self, record_id: str) -> bool:
        stmt = (
            update(self._model)
            .where(self._model.id == record_id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    # ── Count ────────────────────────────────────────────────────────────────

    async def count_by_field(self, field: str, value: Any) -> int:
        from sqlalchemy import func
        col  = getattr(self._model, field)
        stmt = (
            select(func.count())
            .select_from(self._model)
            .where(col == value, self._model.deleted_at.is_(None))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
