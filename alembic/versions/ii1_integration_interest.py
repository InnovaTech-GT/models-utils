"""Integration interest ("Me interesa" / "Sugerir", doc 65 D33)

Revision ID: ii1_integration_interest
Revises: wf2_conversational
Create Date: 2026-11-06

Additive new table. One row per (company, type, provider): the expression
unique index folds a NULL provider (a "Sugerir" row) to '' so NULLs conflict
too. A repeat click bumps updated_at and keeps created_at (ON CONFLICT in the
backend). No CHECK on type: the backend registry is the single list.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "ii1_integration_interest"
down_revision: str | Sequence[str] | None = "wf2_conversational"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text(
        "CREATE TABLE IF NOT EXISTS integration_interest ("
        "id UUID PRIMARY KEY, "
        "created_at TIMESTAMPTZ NOT NULL, "
        "updated_at TIMESTAMPTZ NOT NULL, "
        "company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE, "
        'user_id UUID REFERENCES "user"(id) ON DELETE SET NULL, '
        "provider VARCHAR(32), "
        "type VARCHAR(16) NOT NULL, "
        "note VARCHAR(500))"
    ))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_interest "
        "ON integration_interest (company_id, type, (COALESCE(provider, '')))"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS integration_interest"))
