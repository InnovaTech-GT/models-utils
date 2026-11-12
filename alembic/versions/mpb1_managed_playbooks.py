"""Uplink-managed playbooks: playbook_management, run staff_actor, playbook_revision (doc 70)

Revision ID: mpb1_managed_playbooks
Revises: et1_edge_telnet
Create Date: 2026-11-12

Additive. company.playbook_management (JSON NULL = Uplink-managed),
provisioning_run.staff_actor (NOT NULL DEFAULT false: no superadmin could open
a run before this release), and the playbook_revision edit trail
(playbook_id deliberately has no FK: the trail outlives a deleted playbook).

Data step (one-way): one BASELINE revision per existing playbook, so its first
post-release edit has a "before". Downgrade drops the table and both columns,
losing the edit history (the baseline is recomputable).

Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "mpb1_managed_playbooks"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS playbook_revision (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    playbook_id UUID NOT NULL,
    playbook_name VARCHAR NOT NULL,
    version INTEGER NOT NULL,
    action VARCHAR(16) NOT NULL,
    definition JSON,
    target JSON,
    actor_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
    actor_staff BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_playbook_revision_action
        CHECK (action IN ('BASELINE', 'CREATE', 'UPDATE', 'DELETE', 'BIND', 'UNBIND'))
)
"""

_BASELINE = """
INSERT INTO playbook_revision (id, company_id, playbook_id, playbook_name, version, action,
                               definition, target, actor_user_id, actor_staff, created_at)
SELECT gen_random_uuid(), company_id, id, name, version, 'BASELINE', definition, NULL, NULL, false, now()
FROM playbook
"""


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("ALTER TABLE company ADD COLUMN IF NOT EXISTS playbook_management JSON"))
    c.execute(text("ALTER TABLE provisioning_run ADD COLUMN IF NOT EXISTS staff_actor BOOLEAN NOT NULL DEFAULT false"))
    exists = c.execute(text("SELECT to_regclass('playbook_revision')")).scalar() is not None
    c.execute(text(_CREATE_TABLE))
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_playbook_revision_company_created "
                   "ON playbook_revision (company_id, created_at DESC)"))
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_playbook_revision_playbook "
                   "ON playbook_revision (playbook_id, created_at)"))
    if not exists:  # a re-run never duplicates the baseline
        c.execute(text(_BASELINE))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS playbook_revision"))
    c.execute(text("ALTER TABLE provisioning_run DROP COLUMN IF EXISTS staff_actor"))
    c.execute(text("ALTER TABLE company DROP COLUMN IF EXISTS playbook_management"))
