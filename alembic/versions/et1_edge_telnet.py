"""Telnet on EDGE devices: identity fields, edge_mgmt_cidr, run warnings (doc 67)

Revision ID: et1_edge_telnet
Revises: ch1_client_chat
Create Date: 2026-11-10

Additive, nullable, no backfill. device_type.identity_command/identity_regex
(the identity read the worker runs after an EDGE telnet login),
provisioning_settings.edge_mgmt_cidr (NULL = the feature is off for the
tenant), provisioning_run.warnings (best-effort step warnings). Downgrade drops
the columns. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "et1_edge_telnet"
down_revision: str | Sequence[str] | None = "ch1_client_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("device_type", "identity_command", "VARCHAR(100)"),
    ("device_type", "identity_regex", "VARCHAR(256)"),
    ("provisioning_settings", "edge_mgmt_cidr", "VARCHAR(43)"),
    ("provisioning_run", "warnings", "JSON"),
)


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, column, sql_type in _COLUMNS:
        c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}"))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, column, _ in reversed(_COLUMNS):
        c.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
