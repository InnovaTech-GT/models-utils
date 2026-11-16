"""client_service.network_managed + office-task columns on task (doc 71)

Revision ID: nm1_network_managed
Revises: rf1_referral_programs
Create Date: 2026-11-13

Additive (doc 71 §9.1):
- client_service.network_managed BOOLEAN NOT NULL DEFAULT false: Uplink's
  playbooks manage this service's network configuration, so the
  SUSPENSION / REACTIVATION / DEPROVISION / CPE_SWAP / BOOST playbooks may act
  on it (doc 71 §3).
- task.office_action VARCHAR(20) + ck_task_office_action (an office action
  forces job_kind NULL), task.provisioning_run_id (FK provisioning_run, SET
  NULL, ix_task_provisioning_run_id), task.completed_by (FK "user", SET NULL),
  task.completion_note VARCHAR(500) (doc 71 §4.1).

Backfill (doc 71 §9.2): network_managed = true for a service with a successful
non-dry ACTIVATION / CPE_SWAP / SUSPENSION / REACTIVATION / BOOST run and no
later successful non-dry DEPROVISION. BACKFILL_SQL is a frozen copy of
database_utils.utils.network_managed.backfill_value (revisions never import
evolving utils); tests/test_nm1_network_managed.py runs both on one fixture
set.

Chain: re-chained at compose from et1_edge_telnet onto rf1_referral_programs
(the integration head): down_revision rewritten, never a merge revision.

Inert for old code: the new columns default to false / NULL and old code never
reads them. Downgrade drops the index, CHECK, FKs and columns; nothing is
irreversible. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "nm1_network_managed"
down_revision: str | Sequence[str] | None = "rf1_referral_programs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("client_service", "network_managed", "BOOLEAN NOT NULL DEFAULT false"),
    ("task", "office_action", "VARCHAR(20)"),
    ("task", "provisioning_run_id", "UUID"),
    ("task", "completed_by", "UUID"),
    ("task", "completion_note", "VARCHAR(500)"),
)

# Mirrors database_utils.models.crm._OFFICE_ACTION_CHECK.
_OFFICE_ACTION_CHECK = (
    "office_action IS NULL OR (office_action IN ('NETWORK_CUT','NETWORK_RECONNECT',"
    "'NETWORK_REMOVE','NETWORK_SWAP','RUN_BY_HAND') AND job_kind IS NULL)"
)

_FKS = (
    ("fk_task_provisioning_run_id", "provisioning_run_id", "provisioning_run"),
    ("fk_task_completed_by", "completed_by", '"user"'),
)

# Doc 71 §9.2, frozen (the alias uses AS so the same text runs on SQLite).
BACKFILL_SQL = """
UPDATE client_service AS cs SET network_managed = true
WHERE EXISTS (
  SELECT 1 FROM provisioning_run r
  WHERE r.client_service_id = cs.id AND NOT r.dry_run AND r.status = 'SUCCEEDED'
    AND r.purpose IN ('ACTIVATION','CPE_SWAP','SUSPENSION','REACTIVATION','BOOST')
    AND NOT EXISTS (
      SELECT 1 FROM provisioning_run d
      WHERE d.client_service_id = cs.id AND NOT d.dry_run AND d.status = 'SUCCEEDED'
        AND d.purpose = 'DEPROVISION' AND d.created_at > r.created_at))
"""


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, column, sql_type in _COLUMNS:
        c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}"))
    c.execute(text(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_task_office_action') THEN "
        f"ALTER TABLE task ADD CONSTRAINT ck_task_office_action CHECK ({_OFFICE_ACTION_CHECK}); "
        "END IF; END $$"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_task_provisioning_run_id ON task (provisioning_run_id)"
    ))
    for name, column, target in _FKS:
        c.execute(text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            f"WHERE conname = '{name}') THEN "
            f"ALTER TABLE task ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {target} (id) ON DELETE SET NULL; END IF; END $$"
        ))
    c.execute(text(BACKFILL_SQL))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP INDEX IF EXISTS ix_task_provisioning_run_id"))
    c.execute(text("ALTER TABLE task DROP CONSTRAINT IF EXISTS ck_task_office_action"))
    for name, _, _ in _FKS:
        c.execute(text(f"ALTER TABLE task DROP CONSTRAINT IF EXISTS {name}"))
    for table, column, _ in reversed(_COLUMNS):
        c.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
