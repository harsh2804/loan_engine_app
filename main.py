"""
main.py
───────
FastAPI application entry point.

Startup:
  - Initialises DB tables (dev mode) or runs Alembic (prod)
  - Imports lender strategies (triggers self-registration)

Run:
  uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import get_settings
from database.connection import init_db, close_db

logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level))
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)

    # Import lenders after settings load so `.env` values (e.g. ENABLE_DUMMY_LENDERS)
    # are visible at import-time for self-registration side effects.
    import lenders.strategies  # noqa: F401

    # Create tables in dev mode (use Alembic migrations in production)
    if settings.debug:
        await init_db()
        logger.info("Database tables created (debug mode)")

    from lenders.registry import registry
    logger.info("Registered lenders: %s", registry.list_lenders())

    yield

    await close_db()
    logger.info("Database connections closed")


# ── App ───────────────────────────────────────────────────────────────────────

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "MSME Lending Engine — CIBIL + Account Aggregator + Claude AI.\n\n"
        "Processes loan applications end-to-end with full audit logging."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

from routers.loan import router as loan_router  # noqa: E402
app.include_router(loan_router)


@app.get("/health", tags=["Infra"])
async def health():
    from lenders.registry import registry
    return {
        "status":  "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "lenders": registry.list_lenders(),
    }
