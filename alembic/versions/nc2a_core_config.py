"""cycle 7 (core network configuration): device_category.tier, device_type.cli_platform, inventory_item mgmt surface, topology_device_type pinned item, client_service install state

Revision ID: nc2a_core_config
Revises: nc1b_device_audit_trigger
Create Date: 2026-07-17

Doc 25 (docs/isp-platform/25-cycle7-core-config-design.md) §2. Additive DDL
for the Phase-2 (SSH/Telnet core config) slice + the install state machine:

  - device_category.tier (CORE/EDGE, CHECK ck_device_category_tier) + backfill
    by key: CORE <- ROUTER/SWITCH/OLT; EDGE <- ONU/CPE_ROUTER/ACCESS_POINT;
    others stay NULL (passives). ONU display name -> 'ONU / ONT' (key
    immutable; only rows still carrying the seeded default name are touched,
    so a super-admin rename survives).
  - device_type.cli_platform (netmiko platform id; NULL -> generic drivers).
  - inventory_item management surface: mgmt_host / mgmt_port / cli_protocol
    (CHECK ck_inventory_item_cli_protocol) + worker-stamped
    mgmt_last_check_at / mgmt_last_check_ok.
  - topology_device_type.inventory_item_id FK -> inventory_item ON DELETE
    SET NULL (pinned shared core device per chain position) + index.
  - client_service.install_state (NOT NULL default 'NOT_INSTALLED', CHECK
    ck_client_service_install_state) + installed_at + index
    ix_client_service_company_install_state.

All new value sets are CHECK-constrained strings (c3a/c3b/nc1a precedent —
never ALTER TYPE ADD VALUE). Hand-written (NOT autogenerate): autogenerate
misses CHECK constraints on plain strings and the guarded/idempotent op
style. Additive ops are guarded (IF NOT EXISTS / DROP CONSTRAINT IF EXISTS +
ADD) so a re-run is a no-op where practical, and in-migration assertions
verify each object landed. Backfill UPDATEs are convergent (tier IS NULL /
name = 'ONU' predicates) — a second run changes zero rows.

SQL fragments are duplicated from database_utils/models/isp.py on purpose
(the _CREDENTIAL_KIND_CHECK pattern, nc1a precedent) — kept byte-identical
and guarded by tests/test_core_config_constants.py.

Downgrade totality: drops exactly what it created and reverts the ONU name
(only when still 'ONU / ONT'). The tier/name backfills need no other undo —
the columns drop with their data.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'nc2a_core_config'
down_revision: Union[str, Sequence[str], None] = 'nc1b_device_audit_trigger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQL fragments kept in one place so model + migration agree byte-for-byte
# (mirrors database_utils/models/isp.py).
_DEVICE_CATEGORY_TIER_CHECK = "tier IN ('CORE','EDGE')"
_CLI_PROTOCOL_CHECK = "cli_protocol IN ('ssh','telnet')"
_INSTALL_STATE_CHECK = "install_state IN ('NOT_INSTALLED','IN_PROGRESS','INSTALLED')"

# doc 25 §2.1 backfill map: tier by baseline category key. Others stay NULL.
_TIER_BACKFILL = (
    ("CORE", ("ROUTER", "SWITCH", "OLT")),
    ("EDGE", ("ONU", "CPE_ROUTER", "ACCESS_POINT")),
)

_NEW_COLUMNS = (
    ("device_category", "tier"),
    ("device_type", "cli_platform"),
    ("inventory_item", "mgmt_host"),
    ("inventory_item", "mgmt_port"),
    ("inventory_item", "cli_protocol"),
    ("inventory_item", "mgmt_last_check_at"),
    ("inventory_item", "mgmt_last_check_ok"),
    ("topology_device_type", "inventory_item_id"),
    ("client_service", "install_state"),
    ("client_service", "installed_at"),
)

_NEW_CHECKS = (
    ("device_category", "ck_device_category_tier", _DEVICE_CATEGORY_TIER_CHECK),
    ("inventory_item", "ck_inventory_item_cli_protocol", _CLI_PROTOCOL_CHECK),
    ("client_service", "ck_client_service_install_state", _INSTALL_STATE_CHECK),
)


def _regclass(connection, name: str):
    return connection.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar()


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. additive columns (IF NOT EXISTS = re-run safe) ---
    op.execute("ALTER TABLE device_category ADD COLUMN IF NOT EXISTS tier VARCHAR(10)")
    op.execute("ALTER TABLE device_type ADD COLUMN IF NOT EXISTS cli_platform VARCHAR(50)")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS mgmt_host VARCHAR")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS mgmt_port INTEGER")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS cli_protocol VARCHAR")
    op.execute(
        "ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS mgmt_last_check_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS mgmt_last_check_ok BOOLEAN")
    op.execute("ALTER TABLE topology_device_type ADD COLUMN IF NOT EXISTS inventory_item_id UUID")
    op.execute(
        "ALTER TABLE client_service ADD COLUMN IF NOT EXISTS install_state "
        "VARCHAR(20) NOT NULL DEFAULT 'NOT_INSTALLED'"
    )
    op.execute(
        "ALTER TABLE client_service ADD COLUMN IF NOT EXISTS installed_at "
        "TIMESTAMP WITH TIME ZONE"
    )

    # --- 2. CHECK constraints (DROP IF EXISTS + ADD = re-run safe; NULL
    # passes every one of these by SQL semantics, matching the nullable
    # columns) ---
    for table, name, check in _NEW_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check})")

    # --- 3. pinned-device FK + index (doc 25 §2.4). SET NULL: retiring the
    # shared item must never block; resolution then fails visibly with
    # MISSING_DEVICE. Same-company/type/CORE-tier are router validation. ---
    op.execute(
        "ALTER TABLE topology_device_type "
        "DROP CONSTRAINT IF EXISTS fk_topology_device_type_inventory_item"
    )
    op.execute(
        "ALTER TABLE topology_device_type "
        "ADD CONSTRAINT fk_topology_device_type_inventory_item "
        "FOREIGN KEY (inventory_item_id) REFERENCES inventory_item (id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_topology_device_type_inventory_item_id "
        "ON topology_device_type (inventory_item_id)"
    )

    # --- 4. install-state badge/filter index (doc 25 §2.5) ---
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_client_service_company_install_state "
        "ON client_service (company_id, install_state)"
    )

    # --- 5. data backfills (convergent: second run changes zero rows) ---
    # Tier by baseline key (doc 25 §2.1); tier IS NULL guard preserves any
    # value a super-admin sets between runs.
    for tier, keys in _TIER_BACKFILL:
        op.execute(
            "UPDATE device_category SET tier = '{tier}' "
            "WHERE key IN ({keys}) AND tier IS NULL".format(
                tier=tier, keys=", ".join(f"'{k}'" for k in keys)
            )
        )
    # ONU display name -> 'ONU / ONT' (key immutable). Only rows still named
    # the seeded default are touched — a super-admin rename survives, and the
    # predicate makes the UPDATE idempotent.
    op.execute(
        "UPDATE device_category SET name = 'ONU / ONT' WHERE key = 'ONU' AND name = 'ONU'"
    )

    # --- 6. assertions: every object landed ---
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"nc2a: expected column(s) missing after upgrade: {missing}")
    for _table, name, _check in _NEW_CHECKS:
        exists = connection.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
        ).fetchone()
        if not exists:
            raise RuntimeError(f"nc2a: expected CHECK constraint '{name}' to exist after upgrade")
    for idx in ("ix_topology_device_type_inventory_item_id",
                "ix_client_service_company_install_state"):
        if _regclass(connection, idx) is None:
            raise RuntimeError(f"nc2a: expected index '{idx}' to exist after upgrade")
    fk = connection.execute(text(
        "SELECT 1 FROM pg_constraint "
        "WHERE conname = 'fk_topology_device_type_inventory_item'"
    )).fetchone()
    if not fk:
        raise RuntimeError(
            "nc2a: expected FK 'fk_topology_device_type_inventory_item' to exist after upgrade"
        )

    print("[nc2a_core_config] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Revert the ONU display name only when it still carries this revision's
    # value — a later super-admin rename is preserved (mirrors the guarded
    # upgrade UPDATE).
    op.execute(
        "UPDATE device_category SET name = 'ONU' WHERE key = 'ONU' AND name = 'ONU / ONT'"
    )

    op.execute("DROP INDEX IF EXISTS ix_client_service_company_install_state")
    op.execute("DROP INDEX IF EXISTS ix_topology_device_type_inventory_item_id")
    op.execute(
        "ALTER TABLE topology_device_type "
        "DROP CONSTRAINT IF EXISTS fk_topology_device_type_inventory_item"
    )
    for table, name, _check in _NEW_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")

    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS installed_at")
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS install_state")
    op.execute("ALTER TABLE topology_device_type DROP COLUMN IF EXISTS inventory_item_id")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS mgmt_last_check_ok")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS mgmt_last_check_at")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS cli_protocol")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS mgmt_port")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS mgmt_host")
    op.execute("ALTER TABLE device_type DROP COLUMN IF EXISTS cli_platform")
    op.execute("ALTER TABLE device_category DROP COLUMN IF EXISTS tier")

    print("[nc2a_core_config] downgrade complete")
