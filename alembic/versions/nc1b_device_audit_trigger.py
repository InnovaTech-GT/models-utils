"""cycle 5 phase 1 (device audit hardening): append-only BEFORE UPDATE/DELETE trigger on device_action_log

Revision ID: nc1b_device_audit_trigger
Revises: nc1a_network_config_core
Create Date: 2026-07-07

Doc 23 §2.7, canon C14. device_action_log is append-only; Phase 1 enforces it
AT THE DATABASE with a BEFORE UPDATE OR DELETE trigger that raises an exception.
The app layer additionally exposes read-only list/get (writes happen only in the
worker/backend service modules). SHA-256 row-hash chaining is deferred to
Phase 4 (note only, no DDL here).

Hand-written raw SQL (triggers/functions do not survive autogenerate).
CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS make the upgrade idempotent;
the downgrade drops both trigger and function.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'nc1b_device_audit_trigger'
down_revision: Union[str, Sequence[str], None] = 'nc1a_network_config_core'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        """
        CREATE OR REPLACE FUNCTION device_action_log_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'device_action_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_device_action_log_append_only ON device_action_log")
    op.execute(
        """
        CREATE TRIGGER trg_device_action_log_append_only
            BEFORE UPDATE OR DELETE ON device_action_log
            FOR EACH ROW EXECUTE FUNCTION device_action_log_append_only();
        """
    )

    # Assertion: the trigger is registered.
    exists = connection.execute(text(
        "SELECT 1 FROM pg_trigger WHERE tgname = 'trg_device_action_log_append_only' "
        "AND NOT tgisinternal"
    )).fetchone()
    if not exists:
        raise RuntimeError(
            "nc1b: append-only trigger trg_device_action_log_append_only was not created"
        )
    print("[nc1b_device_audit_trigger] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("DROP TRIGGER IF EXISTS trg_device_action_log_append_only ON device_action_log")
    op.execute("DROP FUNCTION IF EXISTS device_action_log_append_only()")
    print("[nc1b_device_audit_trigger] downgrade complete")
