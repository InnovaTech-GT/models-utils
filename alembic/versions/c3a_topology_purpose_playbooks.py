"""cycle 3 E1: topology_playbook (purpose -> playbook map) + backfill + drop topology.playbook_id

Revision ID: c3a_topology_purpose
Revises: c2e_step_exec_snapshot
Create Date: 2026-07-06

Doc 20-cycle3-design.md E1 + 20a-cycle3-design-appendix.md playbook-purposes
D-E1.0/D-E1.1, normative amendment 1. Topologies move from a single bound
playbook (topology.playbook_id, revision c2c_topology) to a purpose-keyed map
(topology_playbook: one row per (topology, purpose)) — ACTIVATION,
SUSPENSION, REACTIVATION, DEPROVISION, or a tenant-defined custom purpose.

`topology.playbook_id` is DROPPED in this same revision (not deprecated):
prod has ZERO topology rows and zero deployed code reading the column
(pre-ISP head), Cycles 1-3 ship as ONE release, and keeping a NOT NULL dead
column would be worse (double-write or a follow-up relaxing migration for no
benefit). Local/rehearsal rows are preserved by the backfill below — every
existing (topology_id, playbook_id) pair survives as that topology's
ACTIVATION entry.

ACTIVATION is an application-layer invariant (every topology must define
one; enforced in schemas/topology.py, not the DB — PG can't cheaply enforce
"at least one child row"). This is what makes the downgrade below total on
an ACTIVATION-only table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c3a_topology_purpose'
down_revision: Union[str, Sequence[str], None] = 'c2e_step_exec_snapshot'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.create_table(
        'topology_playbook',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('topology_id', sa.Uuid(), nullable=False),
        sa.Column('purpose', sa.String(length=50), nullable=False),
        sa.Column('playbook_id', sa.Uuid(), nullable=False),
        sa.CheckConstraint("purpose ~ '^[A-Z][A-Z0-9_]{0,49}$'", name='ck_topology_playbook_purpose_format'),
        sa.ForeignKeyConstraint(['topology_id'], ['topology.id'], ondelete='CASCADE'),
        # RESTRICT mirrors provisioning_job.playbook_id; playbooks.py's DELETE
        # guard counts these rows (distinct topology_id) alongside jobs.
        sa.ForeignKeyConstraint(['playbook_id'], ['playbook.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('topology_id', 'purpose', name='uq_topology_playbook_purpose'),
    )
    op.create_index('ix_topology_playbook_topology_id', 'topology_playbook', ['topology_id'])
    op.create_index('ix_topology_playbook_playbook_id', 'topology_playbook', ['playbook_id'])

    # Backfill: each existing topology's single playbook becomes its
    # ACTIVATION entry (founder E1). gen_random_uuid() is built-in PG13+
    # (compose + Railway PG qualify). ON CONFLICT is a harmless defensive
    # no-op here (transaction_per_migration means create_table + this INSERT
    # run in one transaction — there is no partial-application state where
    # the conflict target could already hold rows).
    backfilled = connection.execute(text("""
        INSERT INTO topology_playbook (id, created_at, updated_at, topology_id, purpose, playbook_id)
        SELECT gen_random_uuid(), now(), now(), t.id, 'ACTIVATION', t.playbook_id
        FROM topology t
        ON CONFLICT (topology_id, purpose) DO NOTHING
    """)).rowcount
    print(f"[c3a_topology_purpose] backfilled {backfilled} topology row(s) into their ACTIVATION entry")

    # Drop the single-playbook column. PG drops its dependent (unnamed,
    # auto-named) FK along with the column. Prod: zero rows; local/rehearsal
    # rows preserved above.
    op.drop_column('topology', 'playbook_id')
    print("[c3a_topology_purpose] upgrade complete")


def downgrade() -> None:
    """AMENDMENT 1 (doc 20-cycle3-design.md normative amendments #1;
    precedent c2e_step_exec_snapshot's raise-on-unrepresentable-state):
    downgrade is total ONLY on an ACTIVATION-only topology_playbook table.
    Any non-ACTIVATION row (SUSPENSION/REACTIVATION/DEPROVISION/custom
    purposes) cannot be represented by the pre-c3a schema (a single
    topology.playbook_id column) — silently discarding it on downgrade would
    violate ZERO DATA LOSS. Raise instead. Rollback past this revision with
    non-ACTIVATION rows present = restore from the mandatory pre-promote prod
    pg_dump (same precedent as c2e).
    """
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    non_activation = connection.execute(text(
        "SELECT COUNT(*) FROM topology_playbook WHERE purpose <> 'ACTIVATION'"
    )).scalar()
    if non_activation:
        raise RuntimeError(
            f"c3a_topology_purpose downgrade refused: {non_activation} topology_playbook "
            f"row(s) with a non-ACTIVATION purpose exist (SUSPENSION/REACTIVATION/"
            f"DEPROVISION/custom) — the pre-c3a schema (a single topology.playbook_id "
            f"column) cannot represent them. Remove/reassign these rows first, or "
            f"restore from the pre-promote prod pg_dump if rolling back past this revision."
        )

    op.add_column('topology', sa.Column('playbook_id', sa.Uuid(), nullable=True))
    restored = connection.execute(text("""
        UPDATE topology t SET playbook_id = tp.playbook_id
        FROM topology_playbook tp WHERE tp.topology_id = t.id AND tp.purpose = 'ACTIVATION'
    """)).rowcount
    print(f"[c3a_topology_purpose] downgrade: restored playbook_id for {restored} topology row(s) "
          f"from their ACTIVATION entry")

    # Any topology still NULL has zero topology_playbook rows at all — an
    # invariant breach (every topology must have an ACTIVATION entry). Fail
    # loudly rather than leave/force a NOT NULL column with NULLs.
    missing = connection.execute(text(
        "SELECT COUNT(*) FROM topology WHERE playbook_id IS NULL"
    )).scalar()
    if missing:
        raise RuntimeError(
            f"c3a_topology_purpose downgrade: {missing} topology row(s) lack an ACTIVATION "
            f"topology_playbook entry (invariant breach — every topology must have one) — "
            f"cannot restore playbook_id NOT NULL. Fix the data before downgrading."
        )

    op.alter_column('topology', 'playbook_id', nullable=False)
    # None -> PG auto-name, matches c2c_topology's unnamed FK.
    op.create_foreign_key(None, 'topology', 'playbook', ['playbook_id'], ['id'], ondelete='RESTRICT')

    op.drop_index('ix_topology_playbook_playbook_id', table_name='topology_playbook')
    op.drop_index('ix_topology_playbook_topology_id', table_name='topology_playbook')
    op.drop_table('topology_playbook')
    print("[c3a_topology_purpose] downgrade complete")
