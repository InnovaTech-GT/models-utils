"""Custom SNMP device metrics (doc 75)

Revision ID: dm1_device_metrics
Revises: tm2_drop_pppoe_username
Create Date: 2026-11-20

Additive. device_metric (a tenant-defined OID per device type or per device,
exactly one scope, CHECKed here only) and its values device_metric_5m /
device_metric_1h / device_metric_1d (one row per metric, device, instance and
bucket; plain tables, no partitions). Downgrade drops the four tables.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "dm1_device_metrics"
down_revision: str | Sequence[str] | None = "tm2_drop_pppoe_username"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALUES = (
    "metric_id UUID NOT NULL REFERENCES device_metric(id) ON DELETE CASCADE, "
    "inventory_item_id UUID NOT NULL REFERENCES inventory_item(id) ON DELETE CASCADE, "
    "instance VARCHAR(128) NOT NULL DEFAULT '', "
    "{time}, "
    "company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE, "
    "value_avg DOUBLE PRECISION NOT NULL, value_min DOUBLE PRECISION NOT NULL, "
    "value_max DOUBLE PRECISION NOT NULL, samples {samples} NOT NULL, "
    "PRIMARY KEY (metric_id, inventory_item_id, instance, {key})"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS device_metric (
            id UUID PRIMARY KEY,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            name VARCHAR(80) NOT NULL,
            unit VARCHAR(16),
            oid VARCHAR(200) NOT NULL,
            kind VARCHAR(8) NOT NULL DEFAULT 'GAUGE',
            label_oid VARCHAR(200),
            instances JSON NOT NULL DEFAULT '[]'::json,
            subtract_from_oid VARCHAR(200),
            scale DOUBLE PRECISION NOT NULL DEFAULT 1,
            device_type_id UUID REFERENCES device_type(id) ON DELETE CASCADE,
            inventory_item_id UUID REFERENCES inventory_item(id) ON DELETE CASCADE,
            preset VARCHAR(32),
            enabled BOOLEAN NOT NULL DEFAULT true,
            last_poll JSON,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_device_metric_kind CHECK (kind IN ('GAUGE', 'COUNTER')),
            CONSTRAINT ck_device_metric_one_scope
                CHECK ((device_type_id IS NULL) <> (inventory_item_id IS NULL))
        )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_device_metric_company_name "
               "ON device_metric (company_id, name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_device_metric_device_type ON device_metric (device_type_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_device_metric_item ON device_metric (inventory_item_id)")
    for table, time_col, key, samples in (
        ("device_metric_5m", "bucket_start TIMESTAMPTZ NOT NULL", "bucket_start", "SMALLINT"),
        ("device_metric_1h", "bucket_start TIMESTAMPTZ NOT NULL", "bucket_start", "SMALLINT"),
        ("device_metric_1d", "day DATE NOT NULL", "day", "INTEGER"),
    ):
        cols = _VALUES.format(time=time_col, key=key, samples=samples)
        op.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")
        idx = "ix_device_metric_1d_company_day" if key == "day" else f"ix_{table}_company_bucket"
        op.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} (company_id, {key})")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in ("device_metric_1d", "device_metric_1h", "device_metric_5m", "device_metric"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
