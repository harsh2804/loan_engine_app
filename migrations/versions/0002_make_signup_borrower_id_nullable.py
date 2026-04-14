"""Make signups.borrower_id nullable for pre-signup GST verification.

Revision ID: 0002_make_signup_borrower_id_nullable
Revises: 0001_add_signups_table
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_make_signup_borrower_id_nullable"
down_revision = "0001_add_signups_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "signups",
        "borrower_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    # Backfill any NULLs with a dummy value is not possible safely.
    # Downgrade keeps data integrity by refusing if NULLs exist.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT COUNT(*) AS c FROM signups WHERE borrower_id IS NULL")).mappings().one()
    if int(rows["c"]) > 0:
        raise RuntimeError("Cannot downgrade: signups.borrower_id contains NULLs.")

    op.alter_column(
        "signups",
        "borrower_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )

