"""Add borrowers.individual_pan for CIBIL checks.

Revision ID: 0003_add_individual_pan_to_borrowers
Revises: 0002_make_signup_borrower_id_nullable
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_add_individual_pan_to_borrowers"
down_revision = "0002_make_signup_borrower_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("borrowers", sa.Column("individual_pan", sa.String(length=10), nullable=True))
    op.create_index("ix_borrowers_individual_pan", "borrowers", ["individual_pan"])


def downgrade() -> None:
    try:
        op.drop_index("ix_borrowers_individual_pan", table_name="borrowers")
    except Exception:
        pass
    op.drop_column("borrowers", "individual_pan")

