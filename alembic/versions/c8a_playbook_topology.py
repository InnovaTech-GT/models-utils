"""cycle 8 (topology-owned playbooks): drop playbook.target_vendor + target_category_id, add playbook.topology_id

Revision ID: c8a_playbook_topology
Revises: nc2a_core_config
Create Date: 2026-07-17

Doc 26 (docs/isp-platform/26-cycle8-network-ux-design.md) §2. Playbooks become
topology-owned: the topology supplies the device context, so the old
vendor/category targeting columns no longer make sense.

  - DROP playbook.target_vendor and playbook.target_category_id (+ its FK
    fk_playbook_target_category_id, from revision c3b_device_categories).
    Destructive — consuming backend/frontend code ships in the same release
    (Cycles 1-8 have not reached prod), so this is safe.
  - ADD playbook.topology_id UUID FK -> topology.id ON DELETE CASCADE,
    nullable, indexed (ix_playbook_topology_id). NULL = a system/global
    playbook (the seeded per-company core_connectivity_check_*); non-NULL = an
    inline playbook owned by that topology. CASCADE removes the inline playbook
    when the topology is deleted, but does NOT by itself guarantee an
    orphan-free delete: provisioning_job.playbook_id is ON DELETE RESTRICT (NOT
    NULL, and it carries no topology FK), so a topology whose inline playbook
    has run a job (incl. a dry-run) is deletable only after the backend
    topology-delete path first clears the dependent provisioning_job rows. The
    RESTRICT backstop is deliberate (preserves job history).

Hand-written (NOT autogenerate), nc2a_core_config style: guarded/idempotent
ops (DROP ... IF EXISTS, ADD COLUMN IF NOT EXISTS, DROP CONSTRAINT IF EXISTS +
ADD) so a re-run is a no-op, with in-migration assertions verifying each
object's final state. No backfill: existing non-system playbooks are the
Cycle-7 demo ones on develop; topology_id stays NULL until the topology editor
re-saves them (they keep working via TopologyPlaybook meanwhile).

Downgrade totality: re-adds target_vendor / target_category_id (nullable, no
backfill — a partial undo of a destructive drop cannot recover dropped data)
with the RESTRICT FK to device_category restored, and drops topology_id with
its FK + index.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c8a_playbook_topology'
down_revision: Union[str, Sequence[str], None] = 'nc2a_core_config'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# columns this revision removes from playbook (asserted absent after upgrade,
# re-added by downgrade).
_DROPPED_COLUMNS = ("target_vendor", "target_category_id")


def _regclass(connection, name: str):
    return connection.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar()


def _column_exists(connection, table: str, column: str) -> bool:
    return connection.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).fetchone() is not None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. drop the old targeting FK + columns (IF EXISTS = re-run safe) ---
    # The FK from revision c3b_device_categories must go before the column.
    op.execute("ALTER TABLE playbook DROP CONSTRAINT IF EXISTS fk_playbook_target_category_id")
    op.execute("ALTER TABLE playbook DROP COLUMN IF EXISTS target_category_id")
    op.execute("ALTER TABLE playbook DROP COLUMN IF EXISTS target_vendor")

    # --- 2. add topology_id (nullable) + FK (CASCADE) + index (doc 26 §2) ---
    op.execute("ALTER TABLE playbook ADD COLUMN IF NOT EXISTS topology_id UUID")
    op.execute("ALTER TABLE playbook DROP CONSTRAINT IF EXISTS fk_playbook_topology_id")
    op.execute(
        "ALTER TABLE playbook "
        "ADD CONSTRAINT fk_playbook_topology_id "
        "FOREIGN KEY (topology_id) REFERENCES topology (id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_playbook_topology_id ON playbook (topology_id)"
    )

    # --- 3. assertions: final state landed ---
    if not _column_exists(connection, "playbook", "topology_id"):
        raise RuntimeError("c8a: expected column playbook.topology_id after upgrade")
    for column in _DROPPED_COLUMNS:
        if _column_exists(connection, "playbook", column):
            raise RuntimeError(f"c8a: expected column playbook.{column} to be dropped after upgrade")
    fk = connection.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = 'fk_playbook_topology_id'"
    )).fetchone()
    if not fk:
        raise RuntimeError("c8a: expected FK 'fk_playbook_topology_id' after upgrade")
    if _regclass(connection, "ix_playbook_topology_id") is None:
        raise RuntimeError("c8a: expected index 'ix_playbook_topology_id' after upgrade")

    print("[c8a_playbook_topology] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Drop topology_id (index + FK + column).
    op.execute("DROP INDEX IF EXISTS ix_playbook_topology_id")
    op.execute("ALTER TABLE playbook DROP CONSTRAINT IF EXISTS fk_playbook_topology_id")
    op.execute("ALTER TABLE playbook DROP COLUMN IF EXISTS topology_id")

    # Re-add the dropped columns (nullable — the destructive drop's data is
    # gone; this restores the shape, not the values) and the RESTRICT FK to
    # device_category (revision c3b_device_categories).
    op.execute("ALTER TABLE playbook ADD COLUMN IF NOT EXISTS target_vendor VARCHAR")
    op.execute("ALTER TABLE playbook ADD COLUMN IF NOT EXISTS target_category_id UUID")
    op.execute("ALTER TABLE playbook DROP CONSTRAINT IF EXISTS fk_playbook_target_category_id")
    op.execute(
        "ALTER TABLE playbook "
        "ADD CONSTRAINT fk_playbook_target_category_id "
        "FOREIGN KEY (target_category_id) REFERENCES device_category (id) ON DELETE RESTRICT"
    )

    # Assertions: shape restored.
    for column in _DROPPED_COLUMNS:
        if not _column_exists(connection, "playbook", column):
            raise RuntimeError(f"c8a: expected column playbook.{column} restored after downgrade")
    if _column_exists(connection, "playbook", "topology_id"):
        raise RuntimeError("c8a: expected column playbook.topology_id dropped after downgrade")

    print("[c8a_playbook_topology] downgrade complete")
