"""Client chats + integration default / menu flags (doc 65 §6.4, D38, D39)

Revision ID: ch1_client_chat
Revises: ii1_integration_interest
Create Date: 2026-11-06

Additive, no backfill. integration.is_default (explicit "use as default"; the
backend computes the effective default) and integration.menu_enabled (false =
the channel stays silent for unsolicited messages). client_chat links a client
to the chat it verified from; UNIQUE (client_id, integration_id, address) so a
household chat may link to several clients. Downgrade drops the table and the
columns. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "ch1_client_chat"
down_revision: str | Sequence[str] | None = "ii1_integration_interest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("ALTER TABLE integration ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false"))
    c.execute(text("ALTER TABLE integration ADD COLUMN IF NOT EXISTS menu_enabled BOOLEAN NOT NULL DEFAULT true"))
    c.execute(text(
        "CREATE TABLE IF NOT EXISTS client_chat ("
        "id UUID PRIMARY KEY, "
        "created_at TIMESTAMPTZ NOT NULL, "
        "updated_at TIMESTAMPTZ NOT NULL, "
        "company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE, "
        "client_id UUID NOT NULL REFERENCES client(id) ON DELETE CASCADE, "
        "integration_id UUID NOT NULL REFERENCES integration(id) ON DELETE CASCADE, "
        "address VARCHAR(64) NOT NULL, "
        "username VARCHAR(64), "
        "verified_at TIMESTAMPTZ, "
        "last_inbound_at TIMESTAMPTZ)"
    ))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_chat ON client_chat (client_id, integration_id, address)"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_client_chat_address ON client_chat (integration_id, address)"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_client_chat_company_client ON client_chat (company_id, client_id)"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS client_chat"))
    c.execute(text("ALTER TABLE integration DROP COLUMN IF EXISTS menu_enabled"))
    c.execute(text("ALTER TABLE integration DROP COLUMN IF EXISTS is_default"))
