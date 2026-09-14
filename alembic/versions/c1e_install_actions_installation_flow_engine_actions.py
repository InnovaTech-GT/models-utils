"""installation flow: CREATE_ORDER/CREATE_TASK enum values + install-order uniqueness (R-I1)

Revision ID: c1e_install_actions
Revises: c1c_payment_ledger
Create Date: 2026-07-05

Doc 16 §2.4 (R-I1). down_revision is EXPLICITLY the billing branch's R3
('c1c_payment_ledger') to keep one linear revision chain across the two
models-utils branches (§2.6 branch discipline — prevents multiple heads; the
rehearsal asserts `alembic heads` returns exactly 1). The c1c file lives on
feat/billing-rework/models-schema; both branches are always composed into
develop together, billing first.

RENAME COUPLING (verifier note): if the billing branch's R3 revision id
'c1c_payment_ledger' is ever renamed, this file's down_revision MUST be
updated in the same pass — nothing catches the mismatch until compose, where
any alembic command fails with a KeyError on the missing revision. For the
same reason this branch alone cannot run `alembic upgrade` (the c1c file is
absent pre-compose); that is expected.

Seed note (§2.0 rule: every seed change ships with a revision): this branch
also revises alembic/seeds/isp_seed.py (new-installation template v2 +
template upsert). Seeds are applied by env.py after every upgrade, so no seed
call is needed here — this revision carries the DDL the v2 template's steps
depend on.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c1e_install_actions'
down_revision: Union[str, Sequence[str], None] = 'c1c_payment_ledger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Doc 16 §6.h: lock_timeout set in every revision. The CREATE UNIQUE INDEX
    # below (non-CONCURRENTLY) takes a SHARE lock on "order" and would
    # otherwise queue indefinitely behind long transactions during the prod
    # migration.
    op.execute("SET lock_timeout = '5s'")

    # Native-enum ADD VALUE always runs in an autocommit_block (doc 16 §2.0):
    # with transaction_per_migration the new values must be committed before
    # any later statement/seed can reference them, and PG < 12 forbids
    # ADD VALUE inside a transaction block entirely.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE stepactiontype ADD VALUE IF NOT EXISTS 'CREATE_ORDER'")
        op.execute("ALTER TYPE stepactiontype ADD VALUE IF NOT EXISTS 'CREATE_TASK'")

    # DB-level backstop for the CREATE_ORDER idempotency precheck (§5.2): at
    # most one live INSTALLATION order per subscriber service. A genuine race
    # between two workflow executions trips this index and fails the step
    # instead of double-billing the installation fee.
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_order_installation_per_service '
        'ON "order" (client_service_id) '
        "WHERE order_type = 'INSTALLATION' AND status <> 'CANCELLED'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP INDEX IF EXISTS uq_order_installation_per_service')
    # PostgreSQL cannot drop enum values: CREATE_ORDER / CREATE_TASK remain in
    # stepactiontype after downgrade. Documented as irreversible-but-harmless
    # (doc 16 §2.4 / §7): no workflow_step rows reference them once tenants'
    # installed v2 workflows are removed, and orphan enum values are inert.
