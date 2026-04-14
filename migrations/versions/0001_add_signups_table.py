"""Add signups table and move PAN/GSTIN/CIN off borrowers.

Revision ID: 0001_add_signups_table
Revises: None
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0001_add_signups_table"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in set(insp.get_table_names())


def _colnames(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "signups"):
        op.create_table(
            "signups",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("borrower_id", sa.String(length=36), nullable=False, unique=True),
            sa.Column("gstin", sa.String(length=15), nullable=False, unique=True),
            sa.Column("pan", sa.String(length=10), nullable=False, unique=True),
            sa.Column("cin", sa.String(length=30), nullable=True),
            sa.Column("date_of_incorporation", sa.String(length=10), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["borrower_id"], ["borrowers.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_signups_borrower_id", "signups", ["borrower_id"])
        op.create_index("ix_signups_gstin", "signups", ["gstin"])
        op.create_index("ix_signups_pan", "signups", ["pan"])

    if not _has_table(insp, "borrowers"):
        return

    borrower_cols = _colnames(insp, "borrowers")
    legacy_cols = {"gstin", "pan", "cin", "date_of_incorporation"}
    if not legacy_cols.issubset(borrower_cols):
        return

    rows = bind.execute(
        sa.text(
            "SELECT id, gstin, pan, cin, date_of_incorporation, created_at, updated_at, deleted_at "
            "FROM borrowers"
        )
    ).mappings().all()

    for r in rows:
        bind.execute(
            sa.text(
                "INSERT INTO signups "
                "(id, borrower_id, gstin, pan, cin, date_of_incorporation, created_at, updated_at, deleted_at) "
                "VALUES (:id, :borrower_id, :gstin, :pan, :cin, :date_of_incorporation, :created_at, :updated_at, :deleted_at) "
                "ON CONFLICT (borrower_id) DO NOTHING"
            ),
            {
                "id": r["id"],
                "borrower_id": r["id"],
                "gstin": r["gstin"],
                "pan": r["pan"],
                "cin": r["cin"],
                "date_of_incorporation": r["date_of_incorporation"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "deleted_at": r["deleted_at"],
            },
        )

    # Drop non-unique indexes (if present)
    for idx in insp.get_indexes("borrowers"):
        if idx.get("name") in {"ix_borrowers_gstin", "ix_borrowers_pan"}:
            op.drop_index(idx["name"], table_name="borrowers")

    # Drop unique constraints on the legacy columns (names may vary by DB)
    for uc in insp.get_unique_constraints("borrowers"):
        cols = set(uc.get("column_names") or [])
        if cols in [{"gstin"}, {"pan"}]:
            op.drop_constraint(uc["name"], "borrowers", type_="unique")

    # Finally drop columns
    op.drop_column("borrowers", "gstin")
    op.drop_column("borrowers", "pan")
    op.drop_column("borrowers", "cin")
    op.drop_column("borrowers", "date_of_incorporation")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "borrowers"):
        borrower_cols = _colnames(insp, "borrowers")
        if "gstin" not in borrower_cols:
            op.add_column("borrowers", sa.Column("gstin", sa.String(length=15), nullable=False))
            op.create_index("ix_borrowers_gstin", "borrowers", ["gstin"])
            op.create_unique_constraint("uq_borrowers_gstin", "borrowers", ["gstin"])
        if "pan" not in borrower_cols:
            op.add_column("borrowers", sa.Column("pan", sa.String(length=10), nullable=False))
            op.create_index("ix_borrowers_pan", "borrowers", ["pan"])
            op.create_unique_constraint("uq_borrowers_pan", "borrowers", ["pan"])
        if "cin" not in borrower_cols:
            op.add_column("borrowers", sa.Column("cin", sa.String(length=30), nullable=True))
        if "date_of_incorporation" not in borrower_cols:
            op.add_column("borrowers", sa.Column("date_of_incorporation", sa.String(length=10), nullable=True))

    if _has_table(insp, "signups") and _has_table(insp, "borrowers"):
        cols = _colnames(insp, "borrowers")
        if {"gstin", "pan", "cin", "date_of_incorporation"}.issubset(cols):
            rows = bind.execute(
                sa.text(
                    "SELECT borrower_id, gstin, pan, cin, date_of_incorporation FROM signups"
                )
            ).mappings().all()
            for r in rows:
                bind.execute(
                    sa.text(
                        "UPDATE borrowers SET gstin=:gstin, pan=:pan, cin=:cin, date_of_incorporation=:doi "
                        "WHERE id=:borrower_id"
                    ),
                    {
                        "borrower_id": r["borrower_id"],
                        "gstin": r["gstin"],
                        "pan": r["pan"],
                        "cin": r["cin"],
                        "doi": r["date_of_incorporation"],
                    },
                )

    if _has_table(insp, "signups"):
        for idx_name in ("ix_signups_borrower_id", "ix_signups_gstin", "ix_signups_pan"):
            try:
                op.drop_index(idx_name, table_name="signups")
            except Exception:
                pass
        op.drop_table("signups")

