"""
routers package
────────────────
Re-exports all FastAPI router instances.
"""
from routers.loan import router as loan_router

__all__ = ["loan_router"]