"""drop client.installation_status/installation_date + installationstatus enum

Revision ID: cf1_drop_client_install_fields
Revises: ba1_attested_adoption
Create Date: 2026-07-19

Client-install-field removal (feature `client-install-field`, doc 31): the
stored per-client install state was ambiguous under multi-service and a stale
display cache — the truth is per-service (`client_service.install_state`,
nc2a) plus adoption attestation (ba1, doc 30). The clients list/detail now
derive `services_total`/`services_installed` rollups in backend-erp; nothing
stores an aggregate.

One-shot destructive drop is safe THIS release only because prod is
pre-cycle-1 (`a1f2b3c4d5e6`): the chain creates the columns in `cd2f0076c709`
and drops them here in one linear `alembic upgrade head` pass, so no prod
code ever read them.

Data cleanup runs BEFORE the DDL (ordering is load-bearing — an installed
UPDATE_FIELD step writing a dropped column would 500 on its next fire):

  1. Delete `workflow_step` rows whose `action_type='UPDATE_FIELD'` and whose
     `action_config -> 'updates'` references `installation_status` or
     `installation_date` (the seeded new-installation v2 step s3 in installed
     tenant copies). Dependents: `workflow_step_edge` predecessors are
     rerouted to successors THROUGH the deleted step(s) (dedup-guarded; edge
     rows touching the step then go via their ON DELETE CASCADE FKs);
     `workflow_step_execution.step_id` is nullable with ON DELETE SET NULL
     since c2e and keeps its `step_name` snapshot — run history survives by
     design, no orphans, no FK violations.
  2. Delete `insight_chart` rows (Cycle 4) whose clients spec uses the
     `installation_status` dimension or filter — deletion over stripping: a
     stripped spec silently changes the chart's meaning; the dimension no
     longer exists so the chart cannot render at all.

Prod impact of the data pass: none (prod has no ISP workflow templates or
insight charts installed) — it protects local/dev DBs.

Downgrade recreates the enum + columns (NOT NULL DEFAULT 'NOT_INSTALLED',
nullable date) but the data is NOT restorable: previous per-client statuses,
the deleted workflow steps/edges and insight charts are gone permanently.

Hand-written (NOT autogenerate), nc2a/ba1 house style: lock_timeout, guarded
ops, post-upgrade assertions, print markers.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'cf1_drop_client_install_fields'
down_revision: Union[str, Sequence[str], None] = 'ba1_attested_adoption'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned by tests/test_client_install_field_drop.py (c8a precedent).
_DROPPED_COLUMNS = ("installation_status", "installation_date")
_DROPPED_ENUM = "installationstatus"  # exact PG type name from cd2f0076c709

# jsonb key-existence match on the UPDATE_FIELD payload; action_config is a
# JSON column, so the cast is safe (valid JSON guaranteed by the type).
_DOOMED_STEPS_SQL = (
    "SELECT id, workflow_id FROM workflow_step "
    "WHERE action_type = 'UPDATE_FIELD' "
    "AND (action_config::jsonb -> 'updates') ?| ARRAY['installation_status', 'installation_date']"
)


def _cleanup_workflow_steps(connection) -> None:
    doomed_rows = connection.execute(text(_DOOMED_STEPS_SQL)).fetchall()
    if not doomed_rows:
        print("[cf1_drop_client_install_fields] no workflow steps to clean")
        return

    doomed = {row[0] for row in doomed_rows}
    workflow_ids = sorted({str(row[1]) for row in doomed_rows})

    # Reroute edges: connect each deleted step's live predecessors to its live
    # successors, walking THROUGH adjacent doomed steps (handles chains of
    # doomed steps in one pass). Tiny data — Python traversal over the
    # affected workflows' edges is clearer than a recursive CTE.
    edges = connection.execute(
        text(
            "SELECT from_step_id, to_step_id, workflow_id FROM workflow_step_edge "
            "WHERE workflow_id::text = ANY(:workflow_ids)"
        ),
        {"workflow_ids": workflow_ids},
    ).fetchall()

    succ_map: dict = {}
    existing = set()
    for from_id, to_id, wf_id in edges:
        succ_map.setdefault(from_id, []).append(to_id)
        existing.add((from_id, to_id))

    def _live_successors(step_id, seen):
        """Live (non-doomed) steps reachable from step_id via doomed steps only."""
        out = []
        for nxt in succ_map.get(step_id, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            if nxt in doomed:
                out.extend(_live_successors(nxt, seen))
            else:
                out.append(nxt)
        return out

    inserted = 0
    for from_id, to_id, wf_id in edges:
        if from_id in doomed or to_id not in doomed:
            continue  # only live-predecessor -> doomed-step entry edges
        for target in _live_successors(to_id, {to_id}):
            if (from_id, target) in existing:
                continue  # dedupe (also covers a pre-existing direct edge)
            connection.execute(
                text(
                    "INSERT INTO workflow_step_edge (id, created_at, from_step_id, to_step_id, workflow_id) "
                    "VALUES (gen_random_uuid(), NOW(), :from_id, :to_id, :workflow_id)"
                ),
                {"from_id": from_id, "to_id": target, "workflow_id": wf_id},
            )
            existing.add((from_id, target))
            inserted += 1

    # Delete the steps: workflow_step_edge rows touching them CASCADE;
    # workflow_step_execution.step_id SET NULLs, keeping the step_name
    # snapshot (c2e run-history durability) — deliberately NOT deleted.
    connection.execute(
        text("DELETE FROM workflow_step WHERE id::text = ANY(:ids)"),
        {"ids": [str(s) for s in doomed]},
    )
    print(
        f"[cf1_drop_client_install_fields] deleted {len(doomed)} workflow step(s), "
        f"rerouted {inserted} edge(s)"
    )


def _cleanup_insight_charts(connection) -> None:
    result = connection.execute(text(
        "DELETE FROM insight_chart "
        "WHERE (spec::jsonb ->> 'entity') = 'clients' "
        "AND ((spec::jsonb ->> 'dimension') = 'installation_status' "
        "     OR (spec::jsonb -> 'filters') ? 'installation_status')"
    ))
    print(
        f"[cf1_drop_client_install_fields] deleted {result.rowcount} insight chart(s) "
        "referencing installation_status"
    )


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. data cleanup BEFORE the DDL ---
    _cleanup_workflow_steps(connection)
    _cleanup_insight_charts(connection)

    # --- 2. column drops (IF EXISTS = re-run safe) ---
    for column in _DROPPED_COLUMNS:
        op.execute(f"ALTER TABLE client DROP COLUMN IF EXISTS {column}")

    # --- 3. enum type drop (no column uses it anymore) ---
    op.execute(f"DROP TYPE IF EXISTS {_DROPPED_ENUM}")

    # --- 4. assertions: everything is gone ---
    leftover_cols = connection.execute(text(
        "SELECT string_agg(column_name, ', ') FROM information_schema.columns "
        "WHERE table_name = 'client' AND column_name = ANY(:cols)"
    ), {"cols": list(_DROPPED_COLUMNS)}).scalar()
    if leftover_cols:
        raise RuntimeError(
            f"[cf1] expected column(s) still present after upgrade: {leftover_cols}"
        )
    enum_left = connection.execute(
        text("SELECT 1 FROM pg_type WHERE typname = :name"), {"name": _DROPPED_ENUM}
    ).fetchone()
    if enum_left:
        raise RuntimeError(f"[cf1] expected enum type '{_DROPPED_ENUM}' to be dropped")
    doomed_left = connection.execute(text(_DOOMED_STEPS_SQL)).fetchone()
    if doomed_left:
        raise RuntimeError(
            "[cf1] workflow_step rows still reference installation fields after cleanup"
        )
    charts_left = connection.execute(text(
        "SELECT 1 FROM insight_chart "
        "WHERE (spec::jsonb ->> 'entity') = 'clients' "
        "AND ((spec::jsonb ->> 'dimension') = 'installation_status' "
        "     OR (spec::jsonb -> 'filters') ? 'installation_status')"
    )).fetchone()
    if charts_left:
        raise RuntimeError(
            "[cf1] insight_chart rows still reference installation_status after cleanup"
        )

    print("[cf1_drop_client_install_fields] upgrade complete")


def downgrade() -> None:
    # Structure only — per-client statuses, deleted workflow steps/edges and
    # insight charts are NOT restorable (documented in the docstring).
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        "DO $$ BEGIN "
        f"CREATE TYPE {_DROPPED_ENUM} AS ENUM "
        "('NOT_INSTALLED', 'SURVEY_SCHEDULED', 'INSTALL_SCHEDULED', 'INSTALLED', 'CANCELLED'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "ALTER TABLE client ADD COLUMN IF NOT EXISTS installation_status "
        f"{_DROPPED_ENUM} NOT NULL DEFAULT 'NOT_INSTALLED'"
    )
    op.execute(
        "ALTER TABLE client ADD COLUMN IF NOT EXISTS installation_date "
        "TIMESTAMP WITH TIME ZONE"
    )

    print("[cf1_drop_client_install_fields] downgrade complete (data not restored)")
