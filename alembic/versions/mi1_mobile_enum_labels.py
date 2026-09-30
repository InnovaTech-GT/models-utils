"""Mobile integration: enum labels (RELOCATION, DEPOSITED, CONSUMED, RELEASED)

Revision ID: mi1_mobile_enum_labels
Revises: dr1_task_route_sequence, lp1_link_ports
Create Date: 2026-09-29

First of two revisions that put the field apps (uplink-mobile cobros and
tecnicos) on the real system. ALSO THE MERGE POINT of two branches that both
hang off tr1_transport_axis: the fixed-task-status / dispatch-routes cycle
(ts1 -> dr1) and link ports (lp1). Neither touches the other's tables, so the
merge has no ordering concern.

Labels only, and no statement in this file may use them: Postgres forbids
using a label in the transaction that created it (tj1 precedent). mi2 is the
first revision allowed to reference them.

| PG type            | + label             |
|--------------------|---------------------|
| taskjobkind        | RELOCATION          |
| cashsessionstatus  | DEPOSITED           |
| equipmenteventtype | CONSUMED, RELEASED  |

IRREVERSIBLE by nature: Postgres cannot DROP an enum label. `downgrade()` is
a documented no-op (tj1 / c1e precedent).

Hand-written, house style: lock_timeout, IF NOT EXISTS, post-upgrade asserts.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "mi1_mobile_enum_labels"
down_revision: Union[str, Sequence[str], None] = ("dr1_task_route_sequence", "lp1_link_ports")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned by tests/test_mi_field_ops_models.py against the Python enums.
NEW_LABELS = {
    "taskjobkind": ("RELOCATION",),
    "cashsessionstatus": ("DEPOSITED",),
    "equipmenteventtype": ("CONSUMED", "RELEASED"),
}


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    with op.get_context().autocommit_block():
        for pg_type, labels in NEW_LABELS.items():
            for label in labels:
                op.execute(f"ALTER TYPE {pg_type} ADD VALUE IF NOT EXISTS '{label}'")

    for pg_type, labels in NEW_LABELS.items():
        present = set(connection.execute(text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :t"
        ), {"t": pg_type}).scalars())
        missing = [label for label in labels if label not in present]
        if missing:
            raise RuntimeError(f"[mi1] {pg_type} label(s) missing after upgrade: {missing}")

    print("[mi1_mobile_enum_labels] upgrade complete")


def downgrade() -> None:
    # Documented no-op: PostgreSQL cannot DROP an enum label. Rows written
    # with them (a RELOCATION task, a DEPOSITED box) stay readable as long as
    # the Python enums keep the members.
    print("[mi1_mobile_enum_labels] downgrade is a no-op (PG cannot drop enum labels)")
