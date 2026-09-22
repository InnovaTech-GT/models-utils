"""backfill client_service.topology_id from the plan's default topology

Service-lifecycle cycle (founder decision 9): adopted/brownfield services must
be able to have a topology assigned. Before this cycle, topology_id was set
only by the create path — every service imported by the adoption campaign
(doc 30) or created before topologies existed carries NULL. A NULL topology
means no lifecycle purpose can resolve a playbook at all, so the pre-flight
gate blocks suspend / reactivate / cancel, and DELETE refuses non-cancelled
rows: those services are stranded, reachable only through the ADMIN
force-cancel escape hatch (founder decision 2).

This repairs the repairable subset the only way that is safe without guessing:
where the service's plan declares a `default_topology_id`, adopt it.

Data-only — no DDL. Both columns already exist (client_service.topology_id
and service_plan.default_topology_id ship with c8a_playbook_topology).

Idempotent: the `topology_id IS NULL` predicate makes a re-run a no-op, and an
operator who later clears or re-points a topology by hand is never overwritten
by a subsequent `alembic upgrade head`.

Residuals are EXPECTED, not a failure: a service whose plan has no
default_topology_id cannot be repaired by any rule available here (picking an
arbitrary topology would silently provision the wrong device chain). The count
is printed so the operator knows how many rows still need a manual assignment
from the UI, which founder decision 9 makes possible.

Revision ID: bf1_topology_backfill
Revises: sp1_service_params
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'bf1_topology_backfill'
down_revision: Union[str, Sequence[str], None] = 'sp1_service_params'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    before = conn.execute(sa.text(
        "SELECT COUNT(*) FROM client_service WHERE topology_id IS NULL"
    )).scalar()

    result = conn.execute(sa.text(
        "UPDATE client_service SET topology_id = sp.default_topology_id "
        "FROM service_plan sp "
        "WHERE client_service.service_plan_id = sp.id "
        "AND client_service.topology_id IS NULL "
        "AND sp.default_topology_id IS NOT NULL"
    ))

    residual = conn.execute(sa.text(
        "SELECT COUNT(*) FROM client_service WHERE topology_id IS NULL"
    )).scalar()

    print(f"[bf1_topology_backfill] services with NULL topology before: {before}")
    print(f"[bf1_topology_backfill] backfilled from service_plan.default_topology_id: {result.rowcount}")
    print(
        f"[bf1_topology_backfill] residual (still NULL, plan has no default topology — "
        f"assign manually in the UI): {residual}"
    )


def downgrade() -> None:
    # Deliberate no-op. Once written, a backfilled topology_id is
    # indistinguishable from one an operator set by hand — the rows carry no
    # marker recording which pass wrote them. NULLing them back out would
    # therefore destroy real operator assignments made after this revision
    # applied, and re-strand the very services it un-stranded. Leaving the
    # values in place is harmless on downgrade: the column predates this
    # revision and older code reads it exactly the same way.
    pass
