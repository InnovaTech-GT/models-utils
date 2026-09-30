"""Mobile integration: field ops schema (tasks, materials, cash, notifications)

Revision ID: mi2_mobile_field_ops
Revises: mi1_mobile_enum_labels
Create Date: 2026-09-29

Second of two revisions putting uplink-mobile cobros and tecnicos on the real
system. ADDITIVE ONLY: every new column is nullable or has a server default,
and `downgrade()` drops exactly what `upgrade()` added.

- task: started_at, completed_at, step_progress (JSON, default {}).
- task_material (new): materials reported per task, one row per device type.
- inventory_item: latitude, longitude, gps_precision_m. warehouse: latitude,
  longitude. The lot quantity CHECK is deliberately unchanged (an exhausted
  lot becomes status RETIRED).
- user_notification (new; `notification` is the invitation table).
- cash_session: opening_cents, deposited_at, deposited_cents,
  deposit_reference, closed_expected_cash_cents, reopen_count.
- cash_movement (new): top-ups; the client-supplied id is the idempotency key.
- payment: allocation_id, cash_session_id (+ 3 indexes).
- uploaded_file: idempotency_key (+ partial unique per company).
- company: mobile_settings JSON.
- order: partial index ix_order_open_receivables for the cobros list.

Backfills (idempotent, no-ops on empty tables): task.completed_at from
updated_at for DONE tasks; payment.cash_session_id from the collector's box
whose [opened_at, closed_at] window contains paid_at.

RBAC: COLLECTOR gains mobile.collector, tasks.create, service_plans.read,
inventory_items.read (cfg3 pattern). The same entries are in
seeds/isp_seed.ISP_ROLES['COLLECTOR'] — pinned by
tests/test_mobile_rbac_seed.py. No new permission names.

Hand-written, house style: lock_timeout, IF NOT EXISTS, named constraints,
post-upgrade asserts.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "mi2_mobile_field_ops"
down_revision: Union[str, Sequence[str], None] = "mi1_mobile_enum_labels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against isp_seed.ISP_ROLES by tests/test_mobile_rbac_seed.py.
COLLECTOR_GRANTS = (
    "mobile.collector",
    "tasks.create",
    "service_plans.read",
    "inventory_items.read",
)

# Pinned against the model by tests/test_mi_field_ops_models.py.
USER_NOTIFICATION_KIND_CHECK = "kind IN ('TASK_ASSIGNED','TASK_OVERDUE','PAYMENTS_OVERDUE')"
OPEN_RECEIVABLES_WHERE = "status = 'ACTIVE' AND payment_status IN ('PENDING','PARTIAL')"

_ADD_COLUMNS = (
    ("task", "started_at", "TIMESTAMPTZ"),
    ("task", "completed_at", "TIMESTAMPTZ"),
    ("task", "step_progress", "JSON NOT NULL DEFAULT '{}'::json"),
    ("inventory_item", "latitude", "DOUBLE PRECISION"),
    ("inventory_item", "longitude", "DOUBLE PRECISION"),
    ("inventory_item", "gps_precision_m", "DOUBLE PRECISION"),
    ("warehouse", "latitude", "DOUBLE PRECISION"),
    ("warehouse", "longitude", "DOUBLE PRECISION"),
    ("cash_session", "opening_cents", "BIGINT NOT NULL DEFAULT 0"),
    ("cash_session", "deposited_at", "TIMESTAMPTZ"),
    ("cash_session", "deposited_cents", "BIGINT"),
    ("cash_session", "deposit_reference", "VARCHAR(120)"),
    ("cash_session", "closed_expected_cash_cents", "BIGINT"),
    ("cash_session", "reopen_count", "INTEGER NOT NULL DEFAULT 0"),
    ("payment", "allocation_id", "UUID"),
    ("payment", "cash_session_id", "UUID"),
    ("uploaded_file", "idempotency_key", "VARCHAR(80)"),
    ("company", "mobile_settings", "JSON"),
)

_NEW_TABLES = ("task_material", "user_notification", "cash_movement")

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_payment_company_allocation ON payment (company_id, allocation_id)",
    "CREATE INDEX IF NOT EXISTS ix_payment_cash_session ON payment (cash_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_payment_company_received_paid ON payment (company_id, received_by, paid_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_uploaded_file_company_idem ON uploaded_file (company_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    f'CREATE INDEX IF NOT EXISTS ix_order_open_receivables ON "order" (company_id, due_date) WHERE {OPEN_RECEIVABLES_WHERE}',
)
_INDEX_NAMES = (
    "ix_payment_company_allocation", "ix_payment_cash_session", "ix_payment_company_received_paid",
    "uq_uploaded_file_company_idem", "ix_order_open_receivables",
)


def _constraint_exists(connection, name: str) -> bool:
    return connection.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
    ).scalar() is not None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for table, column, ddl in _ADD_COLUMNS:
        op.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {ddl}')

    if not _constraint_exists(connection, "ck_cash_session_opening_nonneg"):
        op.execute("ALTER TABLE cash_session ADD CONSTRAINT ck_cash_session_opening_nonneg CHECK (opening_cents >= 0)")
    if not _constraint_exists(connection, "fk_payment_cash_session_id"):
        op.execute(
            "ALTER TABLE payment ADD CONSTRAINT fk_payment_cash_session_id "
            "FOREIGN KEY (cash_session_id) REFERENCES cash_session (id) ON DELETE SET NULL"
        )

    op.execute("""
        CREATE TABLE IF NOT EXISTS task_material (
            id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            quantity INTEGER NOT NULL,
            consumed_quantity INTEGER NOT NULL DEFAULT 0,
            shortfall INTEGER NOT NULL DEFAULT 0,
            consumed_at TIMESTAMPTZ,
            company_id UUID NOT NULL,
            task_id UUID NOT NULL,
            device_type_id UUID NOT NULL,
            updated_by UUID,
            CONSTRAINT pk_task_material PRIMARY KEY (id),
            CONSTRAINT fk_task_material_company_id FOREIGN KEY (company_id) REFERENCES company (id) ON DELETE CASCADE,
            CONSTRAINT fk_task_material_task_id FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE CASCADE,
            CONSTRAINT fk_task_material_device_type_id FOREIGN KEY (device_type_id) REFERENCES device_type (id) ON DELETE RESTRICT,
            CONSTRAINT fk_task_material_updated_by FOREIGN KEY (updated_by) REFERENCES "user" (id) ON DELETE SET NULL,
            CONSTRAINT ck_task_material_quantity_positive CHECK (quantity > 0),
            CONSTRAINT uq_task_material_task_type UNIQUE (task_id, device_type_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_material_company_task ON task_material (company_id, task_id)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS user_notification (
            id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            kind VARCHAR(32) NOT NULL,
            entity_type VARCHAR(32),
            entity_id UUID,
            dedupe_key VARCHAR(160) NOT NULL,
            payload JSON,
            read_at TIMESTAMPTZ,
            company_id UUID NOT NULL,
            user_id UUID NOT NULL,
            CONSTRAINT pk_user_notification PRIMARY KEY (id),
            CONSTRAINT fk_user_notification_company_id FOREIGN KEY (company_id) REFERENCES company (id) ON DELETE CASCADE,
            CONSTRAINT fk_user_notification_user_id FOREIGN KEY (user_id) REFERENCES "user" (id) ON DELETE CASCADE,
            CONSTRAINT ck_user_notification_kind CHECK ({USER_NOTIFICATION_KIND_CHECK}),
            CONSTRAINT uq_user_notification_dedupe UNIQUE (user_id, dedupe_key)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_notification_feed "
        "ON user_notification (user_id, read_at, created_at DESC)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS cash_movement (
            id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            amount_cents BIGINT NOT NULL,
            note VARCHAR(200),
            company_id UUID NOT NULL,
            cash_session_id UUID NOT NULL,
            created_by UUID,
            CONSTRAINT pk_cash_movement PRIMARY KEY (id),
            CONSTRAINT fk_cash_movement_company_id FOREIGN KEY (company_id) REFERENCES company (id) ON DELETE CASCADE,
            CONSTRAINT fk_cash_movement_cash_session_id FOREIGN KEY (cash_session_id) REFERENCES cash_session (id) ON DELETE CASCADE,
            CONSTRAINT fk_cash_movement_created_by FOREIGN KEY (created_by) REFERENCES "user" (id) ON DELETE SET NULL,
            CONSTRAINT ck_cash_movement_amount_positive CHECK (amount_cents > 0)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_cash_movement_cash_session_id ON cash_movement (cash_session_id)")

    for ddl in _INDEXES:
        op.execute(ddl)

    # --- backfills ---------------------------------------------------------
    op.execute("UPDATE task SET completed_at = updated_at WHERE status = 'DONE' AND completed_at IS NULL")
    op.execute("""
        UPDATE payment p SET cash_session_id = cs.id
        FROM cash_session cs
        WHERE p.cash_session_id IS NULL
          AND p.kind = 'PAYMENT'
          AND p.company_id = cs.company_id
          AND p.received_by = cs.collector_id
          AND p.paid_at >= cs.opened_at
          AND p.paid_at <= COALESCE(cs.closed_at, now())
    """)

    # --- RBAC (cfg3 pattern; env.py re-runs the seeds afterwards anyway) ----
    for perm in COLLECTOR_GRANTS:
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = 'COLLECTOR' AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"perm": perm},
        )

    # --- post-upgrade asserts ----------------------------------------------
    for table, column, _ in _ADD_COLUMNS:
        if connection.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
        ), {"t": table, "c": column}).scalar() is None:
            raise RuntimeError(f"[mi2] {table}.{column} missing after upgrade")
    for table in _NEW_TABLES:
        if connection.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is None:
            raise RuntimeError(f"[mi2] table {table} missing after upgrade")
    for name in _INDEX_NAMES:
        if connection.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is None:
            raise RuntimeError(f"[mi2] index {name} missing after upgrade")

    print("[mi2_mobile_field_ops] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Grants: only the COLLECTOR rows this revision added. The permission rows
    # themselves predate mi2 and stay. (env.py's seed pass re-adds them while
    # the seed file still lists them — cfg3 caveat.)
    for perm in COLLECTOR_GRANTS:
        connection.execute(
            text(
                "DELETE FROM role_permission WHERE "
                "role_id IN (SELECT id FROM role WHERE name = 'COLLECTOR' AND company_id IS NULL) "
                "AND permission_id IN (SELECT id FROM permission WHERE name = :perm)"
            ),
            {"perm": perm},
        )

    for name in _INDEX_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

    op.execute("DROP TABLE IF EXISTS cash_movement")
    op.execute("DROP TABLE IF EXISTS user_notification")
    op.execute("DROP TABLE IF EXISTS task_material")

    op.execute("ALTER TABLE payment DROP CONSTRAINT IF EXISTS fk_payment_cash_session_id")
    op.execute("ALTER TABLE cash_session DROP CONSTRAINT IF EXISTS ck_cash_session_opening_nonneg")

    for table, column, _ in reversed(_ADD_COLUMNS):
        op.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS {column}')

    print("[mi2_mobile_field_ops] downgrade complete")
