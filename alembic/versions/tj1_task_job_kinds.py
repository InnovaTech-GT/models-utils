"""Ordenes de Trabajo: taskjobkind += SUSPEND, MAINTENANCE

Revision ID: tj1_task_job_kinds
Revises: cl1_client_dpi_deactivation
Create Date: 2026-09-16

Figma redesign PR 4 (docs/design/plans/04-ordenes-trabajo.md §2.1, master
plan §4 row 5). The "Tarea" chips on the work-order page offer six job types;
`taskjobkind` only has four (INSTALL / FAULT / CHANGE / REMOVE).

SUSPEND and MAINTENANCE are pure WORK ORDERS: they describe what the
technician goes and does, they do not drive billing (that stays on
client_service) — so nothing else changes here.

`task.job_kind` stays NULLABLE in the database: every legacy row has none and
there is nothing to backfill it from. Required-ness is an API-boundary rule
only (`TaskCreateIn.job_kind` in backend-erp, zod `.min(1)` in the frontend,
`Col("job_kind", required=True)` in the xlsx spec).

This revision adds enum LABELS AND NOTHING ELSE, on purpose. Postgres forbids
using a label in the same transaction that created it, so the columns and the
backfills that would reference SUSPEND/MAINTENANCE live in the next revision
(`tk2_task_links`) — and no statement in this file may name the new labels.

IRREVERSIBLE by nature: Postgres cannot DROP an enum label. `downgrade()` is
a documented no-op, the same shape as `c1e_install_actions`. Two extra labels
nobody references are harmless.

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertion.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'tj1_task_job_kinds'
down_revision: Union[str, Sequence[str], None] = 'cl1_client_dpi_deactivation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_LABELS = ("SUSPEND", "MAINTENANCE")


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Native-enum ADD VALUE always runs in an autocommit_block (c1e
    # precedent): with transaction_per_migration the new values must be
    # committed before any later statement/seed can reference them, and
    # PG < 12 forbids ADD VALUE inside a transaction block entirely.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE taskjobkind ADD VALUE IF NOT EXISTS 'SUSPEND'")
        op.execute("ALTER TYPE taskjobkind ADD VALUE IF NOT EXISTS 'MAINTENANCE'")

    present = set(connection.execute(text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'taskjobkind'"
    )).scalars())
    missing = [label for label in _NEW_LABELS if label not in present]
    if missing:
        raise RuntimeError(
            f"[tj1] taskjobkind label(s) missing after upgrade: {', '.join(missing)}"
        )

    print("[tj1_task_job_kinds] upgrade complete")


def downgrade() -> None:
    # Documented no-op: PostgreSQL cannot DROP an enum label, so SUSPEND and
    # MAINTENANCE remain in taskjobkind after a downgrade (precedent:
    # c1e_install_actions). Harmless — tk2's downgrade has already removed
    # everything that could hold a row referencing them, and the Python enum
    # is the only thing that decides what is writable.
    print("[tj1_task_job_kinds] downgrade is a no-op (PG cannot drop enum labels)")
