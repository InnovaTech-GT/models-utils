"""cycle 10: guard the retired syntax, then drop Topology (doc 35 §8)

Revision ID: ng2_topology_drop
Revises: ng1_network_graph
Create Date: 2026-08-06

The destructive half. Three phases, in this order and no other:

  1. GUARD   — refuse to run at all if anything still depends on the old model
  2. REWRITE — move installed workflows onto the new config key, idempotently
  3. DROP    — remove client_service.topology_id, service_plan.default_topology_id,
               playbook.topology_id, then the three topology tables

WHY THE GUARDS RAISE RATHER THAN REPAIR. Doc 35 forbids a compatibility shim
for the positional variable namespace. A playbook still written against
`chain[n]` would not fail loudly at run time — the renderer's guard would catch
the unrendered token, but only after the job had been queued, claimed and
partially executed. Stopping the release is cheaper than discovering it on a
customer's OLT.

WHY THERE IS NO chain -> graph BACKFILL. A topology names device TYPES; the
graph names device INSTANCES and the physical edges between them. Deriving one
from the other would mean inventing parent relationships — asserting that this
ONT hangs off that splitter when nothing in the database says so. That is
fabricating physical facts about someone's plant, and it is exactly the kind of
"helpful" migration that is discovered six months later when a technician is
sent to the wrong pole. The migration refuses and names the rows instead.

Production reality (verified 2026-08-06 against roundhouse.proxy.rlwy.net):
production is at a1f2b3c4d5e6 with 38 tables and NO ISP schema at all — no
topology, playbook, device_type or client_service. Both guards are therefore
vacuous there; the tables they inspect are created empty by earlier revisions in
this same release chain. They exist for the local and staging databases that DO
carry Cycle 1-9 data, and for any future tenant that gets there first.

Not reversible. downgrade() raises: a graph cannot be turned back into a set of
named chains, and pretending otherwise would silently destroy the plant.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ng2_topology_drop"
down_revision: Union[str, None] = "ng1_network_graph"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every fragment that proves a playbook still speaks the retired dialect.
# RETIRED_ALIAS is pv1's own greppable marker for aliases it could not rewrite;
# it was already meant to fail loudly, and it must not survive this cycle either.
RETIRED_TOKENS = (
    "chain[",
    "edge_devices[",
    "core_devices[",
    "RETIRED_ALIAS",
    "target_position",
)


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables "
        " WHERE table_schema = 'public' AND table_name = :n"
    ), {"n": name}).scalar())


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        " WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).scalar())


def _assert_no_retired_syntax(conn) -> None:
    """Abort the release rather than carry a dead dialect forward."""
    if not _table_exists(conn, "playbook"):
        return
    for token in RETIRED_TOKENS:
        rows = conn.execute(sa.text(
            "SELECT id, name FROM playbook WHERE definition::text LIKE :pat"
        ), {"pat": f"%{token}%"}).fetchall()
        if rows:
            listed = ", ".join(f"{r[0]} ({r[1]})" for r in rows)
            raise RuntimeError(
                f"ng2_topology_drop: playbook(s) still use the retired token "
                f"{token!r}: {listed}. Rewrite them against device.* / cpe.* / "
                f"path.<category>.* (doc 35 §4) before releasing. There is no "
                f"compatibility shim by design."
            )


def _assert_every_service_has_a_cpe(conn) -> None:
    """A service with a topology but no CPE cannot be migrated automatically."""
    if not _table_exists(conn, "client_service"):
        return
    if not _column_exists(conn, "client_service", "topology_id"):
        return
    rows = conn.execute(sa.text(
        "SELECT id FROM client_service "
        " WHERE topology_id IS NOT NULL AND cpe_item_id IS NULL"
    )).fetchall()
    if rows:
        ids = [str(r[0]) for r in rows]
        raise RuntimeError(
            f"ng2_topology_drop: {len(ids)} client_service row(s) have a topology "
            f"but no CPE. There is no automatic chain->graph backfill: a chain "
            f"names device TYPES, a graph names device INSTANCES, and inventing "
            f"parent edges would fabricate physical facts about the plant. "
            f"Assign each service a CPE and attach it to the graph first. "
            f"Ids: {ids[:20]}{' ...' if len(ids) > 20 else ''}"
        )


def _rewrite_workflow_config(conn) -> None:
    """use_topology -> use_service_path, in installed workflows and templates.

    Predicate-guarded on both sides, so a second run matches nothing and leaves
    every row byte-identical. Values are untouched — only the key changes.
    """
    if _column_exists(conn, "workflow_step", "action_config"):
        conn.execute(sa.text("""
            UPDATE workflow_step
               SET action_config = replace(action_config::text,
                                           '"use_topology"',
                                           '"use_service_path"')::json
             WHERE action_config::text LIKE '%use_topology%'
        """))
    if _column_exists(conn, "workflow_template", "definition"):
        conn.execute(sa.text("""
            UPDATE workflow_template
               SET definition = replace(definition::text,
                                        '"use_topology"',
                                        '"use_service_path"')::json
             WHERE definition::text LIKE '%use_topology%'
        """))


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. guards, before anything is touched ----------------------------
    _assert_no_retired_syntax(conn)
    _assert_every_service_has_a_cpe(conn)

    # --- 2. idempotent rewrite --------------------------------------------
    _rewrite_workflow_config(conn)

    # --- 3. drops ----------------------------------------------------------
    # Columns before tables: a referencing FK would block DROP TABLE topology.
    if _column_exists(conn, "client_service", "topology_id"):
        op.drop_index("ix_client_service_topology_id", table_name="client_service")
        op.drop_column("client_service", "topology_id")
    if _column_exists(conn, "service_plan", "default_topology_id"):
        op.drop_column("service_plan", "default_topology_id")
    if _column_exists(conn, "playbook", "topology_id"):
        op.drop_column("playbook", "topology_id")

    for table in ("topology_playbook", "topology_device_type", "topology"):
        if _table_exists(conn, table):
            op.drop_table(table)


def downgrade() -> None:
    raise NotImplementedError(
        "ng2_topology_drop is not reversible. A network graph cannot be turned "
        "back into a set of named device-type chains — the chains carried "
        "per-topology playbook bindings and pinned positions that the graph "
        "does not encode. Restore from a backup taken before the release."
    )
