"""
database.repositories package
───────────────────────────────
Re-exports all repository and UnitOfWork symbols.
"""
from database.repositories.base import BaseRepository
from database.repositories.repositories import (
    BorrowerRepository,
    ApplicationRepository,
    ApiCallLogRepository,
    AuditLogRepository,
    LenderDecisionRepository,
    TransactionLabelRepository,
    UnitOfWork,
)

__all__ = [
    "BaseRepository",
    "BorrowerRepository",
    "ApplicationRepository",
    "ApiCallLogRepository",
    "AuditLogRepository",
    "LenderDecisionRepository",
    "TransactionLabelRepository",
    "UnitOfWork",
]