"""SNMP traffic metrics: per-subscriber rates from the PPPoE queues

Revision ID: sm1_snmp_traffic_metrics
Revises: et1_edge_telnet
Create Date: 2026-11-08

Additive. client_service.pppoe_username (+ partial unique index per company,
backfilled from an existing "pppoe_user" in connection_params or
provisioning_params; on a duplicate inside a company only one service gets
it, preferring a non-cancelled and then the newest one), company.traffic_settings,
and the tables traffic_5m (RANGE-partitioned by bucket_start, one partition
per Guatemala day, no DEFAULT partition), traffic_1h, traffic_1d and
saturation_event. Partitions from yesterday to seven days ahead are created
here; afterwards utils.traffic_partitions keeps them. Downgrade drops
everything this revision added. Hand-written, house style: lock_timeout,
IF [NOT] EXISTS.
"""
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.sql import text

from alembic import op

revision: str = "sm1_snmp_traffic_metrics"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GT = timezone(timedelta(hours=-6))  # America/Guatemala, no DST

_BACKFILL = """
WITH cand AS (
    SELECT cs.id, cs.company_id, cs.status, cs.created_at,
           NULLIF(btrim(COALESCE(
               CASE WHEN json_typeof(cs.connection_params) = 'object'
                    THEN cs.connection_params ->> 'pppoe_user' END,
               (SELECT e ->> 'value'
                  FROM json_array_elements(
                       CASE WHEN json_typeof(cs.provisioning_params) = 'array'
                            THEN cs.provisioning_params ELSE '[]'::json END) e
                 WHERE json_typeof(e) = 'object' AND e ->> 'key' = 'pppoe_user'
                 LIMIT 1)
           )), '') AS u
      FROM client_service cs
     WHERE cs.pppoe_username IS NULL
), ranked AS (
    SELECT id, u,
           row_number() OVER (
               PARTITION BY company_id, u
               ORDER BY (status = 'CANCELLED'), created_at DESC, id
           ) AS rn
      FROM cand
     WHERE u IS NOT NULL AND length(u) <= 64 AND u !~ '[[:space:]<>]'
)
UPDATE client_service cs
   SET pppoe_username = r.u
  FROM ranked r
 WHERE cs.id = r.id AND r.rn = 1
   AND NOT EXISTS (
       SELECT 1 FROM client_service o
        WHERE o.company_id = cs.company_id AND o.pppoe_username = r.u)
"""

_RATES = (
    "down_avg_bps BIGINT NOT NULL, down_max_bps BIGINT NOT NULL, "
    "up_avg_bps BIGINT NOT NULL, up_max_bps BIGINT NOT NULL"
)
_RATES_P95 = (
    "down_avg_bps BIGINT NOT NULL, down_max_bps BIGINT NOT NULL, down_p95_bps BIGINT NOT NULL, "
    "up_avg_bps BIGINT NOT NULL, up_max_bps BIGINT NOT NULL, up_p95_bps BIGINT NOT NULL"
)
_FKS = (
    "client_service_id UUID NOT NULL REFERENCES client_service(id) ON DELETE CASCADE, "
    "company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE"
)


def _check(name: str, table: str, expr: str) -> str:
    return f"CONSTRAINT {name}_{table} CHECK ({expr})"


def _bound(d: date) -> str:
    return datetime.combine(d, time(0), _GT).isoformat()


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))

    # client_service.pppoe_username + backfill + unique per company.
    c.execute(text("ALTER TABLE client_service ADD COLUMN IF NOT EXISTS pppoe_username VARCHAR(64)"))
    c.execute(text(_BACKFILL))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_service_pppoe "
        "ON client_service (company_id, pppoe_username) WHERE pppoe_username IS NOT NULL"
    ))

    c.execute(text("ALTER TABLE company ADD COLUMN IF NOT EXISTS traffic_settings JSON"))

    # traffic_5m: partitioned parent. The PK must include the partition key.
    c.execute(text(
        "CREATE TABLE IF NOT EXISTS traffic_5m ("
        f"{_FKS}, "
        "bucket_start TIMESTAMPTZ NOT NULL, "
        f"{_RATES}, "
        "down_dropped BIGINT NOT NULL DEFAULT 0, up_dropped BIGINT NOT NULL DEFAULT 0, "
        "samples SMALLINT NOT NULL, "
        "PRIMARY KEY (client_service_id, bucket_start), "
        + _check("ck_rates", "traffic_5m",
                 "down_avg_bps >= 0 AND down_max_bps >= down_avg_bps "
                 "AND up_avg_bps >= 0 AND up_max_bps >= up_avg_bps "
                 "AND down_dropped >= 0 AND up_dropped >= 0") + ", "
        + _check("ck_samples", "traffic_5m", "samples BETWEEN 1 AND 5")
        + ") PARTITION BY RANGE (bucket_start)"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_traffic_5m_company_bucket ON traffic_5m (company_id, bucket_start)"
    ))
    today = datetime.now(_GT).date()
    for i in range(-1, 8):
        d = today + timedelta(days=i)
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS traffic_5m_{d:%Y%m%d} PARTITION OF traffic_5m "
            f"FOR VALUES FROM ('{_bound(d)}') TO ('{_bound(d + timedelta(days=1))}')"
        ))

    c.execute(text(
        "CREATE TABLE IF NOT EXISTS traffic_1h ("
        f"{_FKS}, "
        "bucket_start TIMESTAMPTZ NOT NULL, "
        f"{_RATES_P95}, "
        "down_dropped BIGINT NOT NULL DEFAULT 0, up_dropped BIGINT NOT NULL DEFAULT 0, "
        "samples SMALLINT NOT NULL, "
        "PRIMARY KEY (client_service_id, bucket_start), "
        + _check("ck_samples", "traffic_1h", "samples BETWEEN 1 AND 60") + ")"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_traffic_1h_company_bucket ON traffic_1h (company_id, bucket_start)"
    ))

    c.execute(text(
        "CREATE TABLE IF NOT EXISTS traffic_1d ("
        f"{_FKS}, "
        "day DATE NOT NULL, "
        f"{_RATES_P95}, "
        "down_saturated_minutes INTEGER NOT NULL DEFAULT 0, "
        "up_saturated_minutes INTEGER NOT NULL DEFAULT 0, "
        "samples INTEGER NOT NULL, "
        "PRIMARY KEY (client_service_id, day), "
        + _check("ck_minutes", "traffic_1d",
                 "down_saturated_minutes BETWEEN 0 AND 1440 AND up_saturated_minutes BETWEEN 0 AND 1440") + ", "
        + _check("ck_samples", "traffic_1d", "samples BETWEEN 1 AND 1440") + ")"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_traffic_1d_company_day ON traffic_1d (company_id, day)"
    ))

    c.execute(text(
        "CREATE TABLE IF NOT EXISTS saturation_event ("
        "id UUID PRIMARY KEY, "
        "created_at TIMESTAMPTZ NOT NULL, "
        f"{_FKS}, "
        "day DATE NOT NULL, "
        "direction VARCHAR(4) NOT NULL, "
        "plan_mbps INTEGER NOT NULL, "
        "threshold_pct SMALLINT NOT NULL, "
        "saturated_minutes INTEGER NOT NULL, "
        "peak_pct SMALLINT NOT NULL, "
        + _check("ck_direction", "saturation_event", "direction IN ('DOWN', 'UP')") + ", "
        + _check("ck_values", "saturation_event",
                 "plan_mbps > 0 AND threshold_pct BETWEEN 1 AND 100 "
                 "AND saturated_minutes BETWEEN 0 AND 1440 AND peak_pct >= 0") + ")"
    ))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_saturation_event "
        "ON saturation_event (client_service_id, day, direction)"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_saturation_event_company_day ON saturation_event (company_id, day)"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS saturation_event"))
    c.execute(text("DROP TABLE IF EXISTS traffic_1d"))
    c.execute(text("DROP TABLE IF EXISTS traffic_1h"))
    c.execute(text("DROP TABLE IF EXISTS traffic_5m"))  # drops its partitions too
    c.execute(text("ALTER TABLE company DROP COLUMN IF EXISTS traffic_settings"))
    c.execute(text("DROP INDEX IF EXISTS uq_client_service_pppoe"))
    c.execute(text("ALTER TABLE client_service DROP COLUMN IF EXISTS pppoe_username"))
