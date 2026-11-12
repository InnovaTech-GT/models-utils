"""Run triggers: provisioningtrigger labels MCP, BOT, AUTO_SUSPENSION, AUTO_REACTIVATION

Revision ID: rtg1_run_triggers
Revises: et1_edge_telnet
Create Date: 2026-11-11

Doc 68 §3.4. Labels only, and no statement in this file may use them:
Postgres forbids using a label in the transaction that created it (mi1 / tj1
precedent). The one-way backfill of past rows is a separate, later revision
(rtg2_run_trigger_backfill): old processes raise LookupError on a row with a
label they do not know, so rows are rewritten only once every consumer runs
code that knows the labels.

IRREVERSIBLE by nature: Postgres cannot DROP an enum label. `downgrade()` is
a documented no-op.

Hand-written, house style: lock_timeout, IF NOT EXISTS, post-upgrade check.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "rtg1_run_triggers"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Pinned by tests/test_run_triggers.py against ProvisioningTrigger.
NEW_LABELS: tuple[str, ...] = ("MCP", "BOT", "AUTO_SUSPENSION", "AUTO_REACTIVATION")


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    with op.get_context().autocommit_block():
        for label in NEW_LABELS:
            op.execute(f"ALTER TYPE provisioningtrigger ADD VALUE IF NOT EXISTS '{label}'")

    present = set(connection.execute(text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'provisioningtrigger'"
    )).scalars())
    missing = [label for label in NEW_LABELS if label not in present]
    if missing:
        raise RuntimeError(f"[rtg1] provisioningtrigger label(s) missing after upgrade: {missing}")

    print("[rtg1_run_triggers] upgrade complete")


def downgrade() -> None:
    # Documented no-op: PostgreSQL cannot DROP an enum label. Rows written with
    # them stay readable as long as ProvisioningTrigger keeps the members.
    print("[rtg1_run_triggers] downgrade is a no-op (PG cannot drop enum labels)")
