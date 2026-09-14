"""cycle 2 run-history durability: workflow_step_execution.step_name + nullable step_id

Revision ID: c2e_step_exec_snapshot
Revises: c2d_graph_removal
Create Date: 2026-07-06

Doc 18-cycle2-design.md D9 + 18a-cycle2-design-appendix.md automations-ux §2
Fix B. Two pre-existing durability defects (backend-erp/routers/workflows.py
delete_step, and any future step-deleting operation) currently destroy run
history: workflow_step_execution.step_id is NOT NULL with ON DELETE CASCADE,
so deleting one step erases every WorkflowStepExecution row that ever ran it.

Fix: step_id becomes nullable with ON DELETE SET NULL, and a step_name
snapshot column is added (backfilled from the CURRENT workflow_step.name —
best-effort; a step renamed since its last run backfills with its current
name, not the historical one, which is the best available signal at
migration time). Going forward, workflow_engine.execute_workflow stamps
step_name at execution time, so future rows always carry a proper snapshot
regardless of later renames/deletes.

Irreversible (precedent: c1e_install_actions's ALTER TYPE ADD VALUE):
restoring NOT NULL + CASCADE fails outright for any row whose step_id is
already NULL (a step deleted after this migration), and dropping step_name
destroys engine-written execution-time snapshots that cannot be reproduced by
re-backfilling (that would copy the CURRENT step name, which may differ from
what actually ran, or fail entirely for a since-deleted step). downgrade()
raises; rollback past this revision = restore from the mandatory pre-promote
prod pg_dump.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c2e_step_exec_snapshot'
down_revision: Union[str, Sequence[str], None] = 'c2d_graph_removal'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.add_column('workflow_step_execution', sa.Column('step_name', sa.String(), nullable=True))

    backfilled = connection.execute(text("""
        UPDATE workflow_step_execution wse SET step_name = ws.name
        FROM workflow_step ws
        WHERE wse.step_id = ws.id AND wse.step_name IS NULL
    """)).rowcount
    print(f"[c2e_step_exec_snapshot] backfilled step_name for {backfilled} row(s) "
          f"from the CURRENT workflow_step.name (best-effort; a step renamed since "
          f"its last run backfills with its current name)")

    op.drop_constraint('workflow_step_execution_step_id_fkey', 'workflow_step_execution', type_='foreignkey')
    op.alter_column('workflow_step_execution', 'step_id', existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        'workflow_step_execution_step_id_fkey', 'workflow_step_execution', 'workflow_step',
        ['step_id'], ['id'], ondelete='SET NULL',
    )

    missing = connection.execute(text("""
        SELECT COUNT(*) FROM workflow_step_execution wse
        WHERE wse.step_id IS NOT NULL
          AND wse.step_name IS NULL
          AND NOT EXISTS (SELECT 1 FROM workflow_step ws WHERE ws.id = wse.step_id)
    """)).scalar()
    if missing:
        print(f"[c2e_step_exec_snapshot] WARNING: {missing} row(s) reference a step_id "
              f"whose workflow_step row is already gone yet FK still enforced NOT NULL "
              f"until now — should be impossible pre-migration; investigate before promote")
    print("[c2e_step_exec_snapshot] upgrade complete")


def downgrade() -> None:
    """Irreversible (doc 18 amendment 13, precedent c1e_install_actions):
    NULL step_id rows are history snapshots for steps that no longer exist —
    restoring NOT NULL would fail outright (or force a destructive DELETE,
    which violates ZERO DATA LOSS), and step_name is an engine-written
    execution-time snapshot a backfill cannot reproduce. Rollback past this
    revision = restore from the mandatory pre-promote prod pg_dump."""
    raise RuntimeError(
        "c2e_step_exec_snapshot downgrade is irreversible: step_name snapshots "
        "and NULL step_id rows cannot be restored — restore from the pre-promote "
        "prod pg_dump instead (doc 18 amendment 13; precedent c1e_install_actions)"
    )
