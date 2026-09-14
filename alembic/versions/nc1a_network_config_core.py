"""cycle 5 phase 1 (network config core): device_credential, network_access, acs_device_registration, provisioning_settings, device_action_log + columns + PENDING_INFORM + provisioning_job index rebuilds

Revision ID: nc1a_network_config_core
Revises: c4b_drop_installation_address
Create Date: 2026-07-07

Doc 23 (docs/isp-platform/23-network-config-implementation-plan.md) §2, canon
C1/C2/C6/C7/C9/C11/C13/C14/C19. Additive Phase-1 DDL for the TR-069 / GenieACS
network-configuration layer:

  - 5 new tables: network_access (C9), device_credential (C1/C19),
    acs_device_registration (C13), provisioning_settings (C6), device_action_log
    (C14 — the append-only ENFORCEMENT trigger ships in nc1b, not here).
  - new columns on existing tables: inventory_item.oui (C13),
    device_type.provisioning_enabled (C6), client_service.provisioning_state,
    playbook.last_dry_run_version (C7), provisioning_job.dry_run /
    pending_step_index / pending_task_ids / heartbeat_at / device_lock_key.
  - PENDING_INFORM added to the provisioningjobstatus PG enum (C2) via
    ALTER TYPE ADD VALUE in an autocommit block — done FIRST so the recreated
    partial index predicates below may reference it in the same upgrade.
  - provisioning_job partial indexes: uq_provisioning_job_company_idem's
    predicate is extended to include PENDING_INFORM (C2), and the new
    uq_provisioning_job_device_lock serialization index is created (C11).

Hand-written (NOT autogenerate): autogenerate misses partial-index predicates,
CHECK constraints on plain strings, autocommit enum blocks, and server_default
nuances. Additive ops are guarded (to_regclass / IF NOT EXISTS) so a re-run is
a no-op where practical, and in-migration assertions verify each object landed.

Downgrade totality: drops exactly what it created. Documented exception (C2):
the PENDING_INFORM enum value cannot be removed from the PG type without a type
rebuild — downgrade remaps any parked rows (SET status='RUNNING') and leaves the
value in place (harmless inert orphan).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'nc1a_network_config_core'
down_revision: Union[str, Sequence[str], None] = 'c4b_drop_installation_address'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQL fragments kept in one place so model + migration agree byte-for-byte
# (mirrors database_utils/models/isp.py).
_CREDENTIAL_KIND_CHECK = (
    "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST',"
    "'HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT')"
)
_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','olt')"
_NETWORK_ACCESS_MODE_CHECK = "mode IN ('direct','vpn','tunnel')"

_IN_FLIGHT = "status IN ('QUEUED','RUNNING','PENDING_INFORM')"      # after nc1a
_IN_FLIGHT_PRE = "status IN ('QUEUED','RUNNING')"                   # before nc1a

_NEW_TABLES = (
    "network_access", "device_credential", "acs_device_registration",
    "provisioning_settings", "device_action_log",
)


def _regclass(connection, name: str):
    return connection.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar()


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. PENDING_INFORM enum value FIRST (autocommit block, canon C2) ---
    # transaction_per_migration=True (env.py): a newly added enum value must be
    # committed before any later statement (the index predicates below) can
    # reference it. autocommit_block commits it and returns to the migration txn.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE provisioningjobstatus ADD VALUE IF NOT EXISTS 'PENDING_INFORM'")

    # --- 2. network_access (created before device_credential — FK target) ---
    if _regclass(connection, "network_access") is None:
        op.create_table(
            "network_access",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False, server_default="direct"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("mgmt_subnets", sa.JSON(), nullable=True),
            sa.Column("acs_base_url", sa.String(), nullable=True),
            sa.Column("company_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "name", name="uq_network_access_company_name"),
            sa.CheckConstraint(_NETWORK_ACCESS_KIND_CHECK, name="ck_network_access_kind"),
            sa.CheckConstraint(_NETWORK_ACCESS_MODE_CHECK, name="ck_network_access_mode"),
        )
        op.create_index("ix_network_access_company_id", "network_access", ["company_id"])
        # Exactly one default path per tenant PER KIND (canon C9).
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_network_access_default "
            "ON network_access (company_id, kind) WHERE is_default"
        )

    # --- 3. device_credential (canon C1/C19) ---
    if _regclass(connection, "device_credential") is None:
        op.create_table(
            "device_credential",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("username", sa.String(), nullable=True),
            sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
            sa.Column("dek_wrapped", sa.LargeBinary(), nullable=False),
            sa.Column("kek_id", sa.String(), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=True),
            sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("company_id", sa.Uuid(), nullable=False),
            sa.Column("inventory_item_id", sa.Uuid(), nullable=True),
            sa.Column("device_type_id", sa.Uuid(), nullable=True),
            sa.Column("network_access_id", sa.Uuid(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_item.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["device_type_id"], ["device_type.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["network_access_id"], ["network_access.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "name", name="uq_device_credential_company_name"),
            sa.CheckConstraint(_CREDENTIAL_KIND_CHECK, name="ck_device_credential_kind"),
        )
        op.create_index("ix_device_credential_company_id", "device_credential", ["company_id"])
        op.create_index("ix_device_credential_inventory_item_id", "device_credential", ["inventory_item_id"])
        op.create_index("ix_device_credential_device_type_id", "device_credential", ["device_type_id"])
        op.create_index("ix_device_credential_network_access_id", "device_credential", ["network_access_id"])

    # --- 4. acs_device_registration (canon C13; company_id NULLABLE) ---
    if _regclass(connection, "acs_device_registration") is None:
        op.create_table(
            "acs_device_registration",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Uuid(), nullable=True),
            sa.Column("inventory_item_id", sa.Uuid(), nullable=True),
            sa.Column("serial_number", sa.String(), nullable=False),
            sa.Column("oui", sa.String(), nullable=True),
            sa.Column("first_inform_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_inform_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("genieacs_device_id", sa.String(), nullable=True),
            sa.Column("cwmp_cr_username", sa.String(), nullable=True),
            sa.Column("cwmp_cr_secret_ciphertext", sa.LargeBinary(), nullable=True),
            sa.Column("cwmp_cr_dek_wrapped", sa.LargeBinary(), nullable=True),
            sa.Column("cwmp_cr_kek_id", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_item.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            # GLOBAL unique (no company_id): unambiguous first-inform claim.
            sa.UniqueConstraint("oui", "serial_number", name="uq_acs_registration_identity"),
        )
        op.create_index("ix_acs_device_registration_company_id", "acs_device_registration", ["company_id"])

    # --- 5. provisioning_settings (canon C6; singleton per tenant) ---
    if _regclass(connection, "provisioning_settings") is None:
        op.create_table(
            "provisioning_settings",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Uuid(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("default_inform_interval", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", name="uq_provisioning_settings_company"),
        )

    # --- 6. device_action_log (canon C14; trigger enforcement in nc1b) ---
    if _regclass(connection, "device_action_log") is None:
        op.create_table(
            "device_action_log",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Uuid(), nullable=False),
            sa.Column("actor_user_id", sa.Uuid(), nullable=True),
            sa.Column("actor_kind", sa.String(), nullable=False),
            sa.Column("device_kind", sa.String(), nullable=True),
            sa.Column("device_identity", sa.String(), nullable=True),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("before_data", sa.JSON(), nullable=True),
            sa.Column("after_data", sa.JSON(), nullable=True),
            sa.Column("provisioning_job_id", sa.Uuid(), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["provisioning_job_id"], ["provisioning_job.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_device_action_log_company_created",
            "device_action_log", ["company_id", "created_at"],
        )

    # --- 7. additive columns on existing tables (IF NOT EXISTS = re-run safe) ---
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS oui VARCHAR")
    op.execute(
        "ALTER TABLE device_type ADD COLUMN IF NOT EXISTS provisioning_enabled "
        "BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE client_service ADD COLUMN IF NOT EXISTS provisioning_state JSON")
    op.execute("ALTER TABLE playbook ADD COLUMN IF NOT EXISTS last_dry_run_version INTEGER")
    op.execute(
        "ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS dry_run "
        "BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS pending_step_index INTEGER")
    op.execute("ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS pending_task_ids JSON")
    op.execute(
        "ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS heartbeat_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS device_lock_key VARCHAR")

    # --- 8. provisioning_job partial index rebuilds (canon C2/C11) ---
    # Extend the idempotency predicate to include PENDING_INFORM (drop+recreate:
    # a partial index predicate cannot be altered in place).
    op.execute("DROP INDEX IF EXISTS uq_provisioning_job_company_idem")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_job_company_idem "
        "ON provisioning_job (company_id, idempotency_key) "
        f"WHERE idempotency_key IS NOT NULL AND {_IN_FLIGHT}"
    )
    # New per-device serialization index (canon C11).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_job_device_lock "
        "ON provisioning_job (device_lock_key) "
        f"WHERE device_lock_key IS NOT NULL AND {_IN_FLIGHT}"
    )

    # --- 9. assertions: every object landed ---
    for tbl in _NEW_TABLES:
        if _regclass(connection, tbl) is None:
            raise RuntimeError(f"nc1a: expected table '{tbl}' to exist after upgrade")
    for idx in ("uq_provisioning_job_company_idem", "uq_provisioning_job_device_lock",
                "uq_network_access_default", "uq_acs_registration_identity"):
        if _regclass(connection, idx) is None:
            raise RuntimeError(f"nc1a: expected index/constraint '{idx}' to exist after upgrade")
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        "  ('inventory_item','oui'), ('device_type','provisioning_enabled'), "
        "  ('client_service','provisioning_state'), ('playbook','last_dry_run_version'), "
        "  ('provisioning_job','dry_run'), ('provisioning_job','pending_step_index'), "
        "  ('provisioning_job','pending_task_ids'), ('provisioning_job','heartbeat_at'), "
        "  ('provisioning_job','device_lock_key') "
        ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"nc1a: expected column(s) missing after upgrade: {missing}")

    print("[nc1a_network_config_core] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # PENDING_INFORM cannot be removed from the PG enum without a type rebuild
    # (canon C2). Remap any parked rows to RUNNING so the restored idempotency
    # index predicate (QUEUED/RUNNING only) stays valid; the enum value remains
    # as an inert orphan.
    op.execute("UPDATE provisioning_job SET status = 'RUNNING' WHERE status = 'PENDING_INFORM'")

    op.execute("DROP INDEX IF EXISTS uq_provisioning_job_device_lock")
    op.execute("DROP INDEX IF EXISTS uq_provisioning_job_company_idem")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_job_company_idem "
        "ON provisioning_job (company_id, idempotency_key) "
        f"WHERE idempotency_key IS NOT NULL AND {_IN_FLIGHT_PRE}"
    )

    op.execute("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS device_lock_key")
    op.execute("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS heartbeat_at")
    op.execute("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS pending_task_ids")
    op.execute("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS pending_step_index")
    op.execute("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS dry_run")
    op.execute("ALTER TABLE playbook DROP COLUMN IF EXISTS last_dry_run_version")
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS provisioning_state")
    op.execute("ALTER TABLE device_type DROP COLUMN IF EXISTS provisioning_enabled")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS oui")

    # Drop tables in FK-safe order (device_credential before network_access).
    op.execute("DROP TABLE IF EXISTS device_action_log")
    op.execute("DROP TABLE IF EXISTS provisioning_settings")
    op.execute("DROP TABLE IF EXISTS acs_device_registration")
    op.execute("DROP TABLE IF EXISTS device_credential")
    op.execute("DROP TABLE IF EXISTS network_access")

    print("[nc1a_network_config_core] downgrade complete")
