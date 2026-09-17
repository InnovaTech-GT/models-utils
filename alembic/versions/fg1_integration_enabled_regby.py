"""Figma follow-ups: integration.enabled/provider + acs_device_registration.created_by_user_id

Revision ID: fg1_integration_enabled_regby
Revises: ng2_provisioning_run_list
Create Date: 2026-09-17

Backend follow-ups surfaced by the Settings (Integraciones, Red) redesign:

  - integration.enabled BOOLEAN NOT NULL DEFAULT true — the UI "disconnects"
    an integration by PATCHing enabled=false instead of deleting it (keeps
    credentials). Disabled integrations are refused by provisioning.
  - integration.provider VARCHAR NULL — optional well-known provider tag
    (today only WHATSAPP_BUSINESS) so the UI can render a branded card.
    Backfilled from base_url for existing Meta Graph API rows.
  - acs_device_registration.created_by_user_id UUID NULL, FK
    fk_acs_device_registration_created_by_user -> "user"(id) ON DELETE SET NULL
    — "registered by" in the ACS device list. NULL for bootstrap/quarantine
    rows (no human author) and for every pre-existing row.

Id kept short on purpose: alembic_version.version_num is VARCHAR(32).

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, guarded
idempotent ops, post-upgrade assertions, total downgrade (drops the three
columns with their data).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'fg1_integration_enabled_regby'
down_revision: Union[str, Sequence[str], None] = 'ng2_provisioning_run_list'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = (
    ("integration", "enabled"),
    ("integration", "provider"),
    ("acs_device_registration", "created_by_user_id"),
)
_FK = "fk_acs_device_registration_created_by_user"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. integration state + provider (IF NOT EXISTS = re-run safe) ---
    op.execute(
        "ALTER TABLE integration ADD COLUMN IF NOT EXISTS enabled "
        "BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE integration ADD COLUMN IF NOT EXISTS provider VARCHAR")
    op.execute(
        "UPDATE integration SET provider = 'WHATSAPP_BUSINESS' "
        "WHERE provider IS NULL AND base_url ILIKE '%graph.facebook.com%'"
    )

    # --- 2. registration author (SET NULL: deleting a user keeps the row) ---
    op.execute(
        "ALTER TABLE acs_device_registration "
        "ADD COLUMN IF NOT EXISTS created_by_user_id UUID"
    )
    op.execute(f"ALTER TABLE acs_device_registration DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(
        f"ALTER TABLE acs_device_registration ADD CONSTRAINT {_FK} "
        'FOREIGN KEY (created_by_user_id) REFERENCES "user" (id) ON DELETE SET NULL'
    )

    # --- 3. assertions ---
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[fg1] expected column(s) missing after upgrade: {missing}")
    if not connection.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": _FK}
    ).fetchone():
        raise RuntimeError(f"[fg1] expected FK '{_FK}' to exist after upgrade")

    print("[fg1_integration_enabled_regby] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(f"ALTER TABLE acs_device_registration DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute("ALTER TABLE acs_device_registration DROP COLUMN IF EXISTS created_by_user_id")
    op.execute("ALTER TABLE integration DROP COLUMN IF EXISTS provider")
    op.execute("ALTER TABLE integration DROP COLUMN IF EXISTS enabled")

    print("[fg1_integration_enabled_regby] downgrade complete")
