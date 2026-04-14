"""
config/settings.py
──────────────────
All environment-driven configuration.  A single Settings object is
instantiated once and injected everywhere via FastAPI's Depends().

Never import settings directly — always inject via get_settings().
"""
from __future__ import annotations
from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_name: str       = "Multi-Lender Loan Engine"
    app_version: str    = "2.0.0"
    debug: bool         = False
    log_level: str      = "INFO"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:password@localhost:5432/loan_engine",
        description="Async SQLAlchemy connection string",
    )
    db_pool_size: int         = 10
    db_max_overflow: int      = 20
    db_pool_recycle_seconds: int = 3600

    # ── Anthropic ────────────────────────────────────────────────────────────
    anthropic_api_key: str    = Field(..., description="Anthropic API key")
    claude_model: str         = "claude-haiku-4-5-20251001"
    claude_batch_size: int    = 200
    claude_timeout_seconds: int = 60

    # ── Surepass (CIBIL + AA) ────────────────────────────────────────────────
    surepass_base_url: str    = "https://kyc-api.surepass.app"
    surepass_aa_base_url: str = "https://kyc-api.surepass.app"
    surepass_jwt_token: str   = Field(..., description="Bearer token for Surepass APIs")
    surepass_timeout_seconds: int = 30
    surepass_max_retries: int = 1
    surepass_retry_backoff: float = 1.5   # seconds, multiplied on each retry

    # —— Capaxis GST verify ——––––––––––––––––––––––––––––––––––––––––––––––––––
    capaxis_base_url: str = "https://api.capaxis.co.in"
    capaxis_timeout_seconds: int = 30
    capaxis_max_retries: int = 1
    capaxis_retry_backoff: float = 1.5

    # —— Attestr MCA suite ——–––––––––––––––––––––––––––––––––––––––––––––––––––
    attestr_base_url: str = "https://dashboard.attestr.com"
    attestr_public_base_url: str = "https://api.attestr.com"
    attestr_auth_token: str | None = Field(
        default=None,
        description="Basic auth token for Attestr MCA APIs",
    )
    attestr_basic_auth_token: str | None = Field(
        default=None,
        description="Base64 credential for Attestr Basic auth (preferred for MCA GSTIN-to-CIN lookup)",
    )
    attestr_timeout_seconds: int = 30
    attestr_max_retries: int = 1
    attestr_retry_backoff: float = 1.5

    # ── AA polling ───────────────────────────────────────────────────────────
    aa_poll_interval_seconds: int = 5
    aa_poll_max_attempts: int     = 24   # 24 × 5s = 2 minutes max wait

    @field_validator("database_url")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if not v.startswith(("postgresql", "sqlite")):
            raise ValueError("Only PostgreSQL or SQLite supported")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — safe to call anywhere."""
    return Settings()
