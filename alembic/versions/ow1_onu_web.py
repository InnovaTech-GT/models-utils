"""onu_web driver: CPE_WEB credentials + priority, cpe_web_cidrs, onu_config_backup (doc 77)

Revision ID: ow1_onu_web
Revises: dm1_device_metrics
Create Date: 2026-11-24

Additive (models-utils 6.40.0):

device_credential      + priority INTEGER NOT NULL DEFAULT 0 (metadata-only on PG 11+);
                         ck_device_credential_kind re-created with 'CPE_WEB'
provisioning_settings  + cpe_web_cidrs TEXT NULL (NULL = onu_web refuses every host)
onu_config_backup      new: an ONU's config file, envelope-encrypted with
                         device_credential's column names; index
                         (company_id, inventory_item_id, created_at)

Chained on dm1_device_metrics, the head of this branch; re-chain onto
sec1_quantity_checks (v1.14.0) before composing into develop.

The CHECK literals are this revision's own copies (revisions are immutable);
tests/test_ow1_onu_web.py pins _CREDENTIAL_KIND_CHECK byte-identical to
database_utils/models/isp.py and _CREDENTIAL_KIND_CHECK_PRE to pe1's.
Downgrade refuses while a CPE_WEB credential exists (the old CHECK would
reject it), then drops the table and the columns.

Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "ow1_onu_web"
down_revision: str | Sequence[str] | None = "dm1_device_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREDENTIAL_KIND_CHECK = "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST','HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT','CLI_ENABLE','CPE_WEB')"
_CREDENTIAL_KIND_CHECK_PRE = "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST','HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT','CLI_ENABLE')"


def _set_kind_check(c, check: str) -> None:
    c.execute(text(
        "ALTER TABLE device_credential DROP CONSTRAINT IF EXISTS ck_device_credential_kind"
    ))
    c.execute(text(
        "ALTER TABLE device_credential ADD CONSTRAINT ck_device_credential_kind "
        f"CHECK ({check})"
    ))


def upgrade() -> None:
    c = op.get_bind()
    # The live worker polls provisioning tables every second; never queue behind it.
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text(
        "ALTER TABLE device_credential "
        "ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0"
    ))
    _set_kind_check(c, _CREDENTIAL_KIND_CHECK)
    c.execute(text(
        "ALTER TABLE provisioning_settings ADD COLUMN IF NOT EXISTS cpe_web_cidrs TEXT NULL"
    ))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS onu_config_backup (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            inventory_item_id UUID NOT NULL REFERENCES inventory_item(id) ON DELETE CASCADE,
            provisioning_run_id UUID REFERENCES provisioning_run(id) ON DELETE SET NULL,
            provisioning_job_id UUID REFERENCES provisioning_job(id) ON DELETE SET NULL,
            credential_id UUID REFERENCES device_credential(id) ON DELETE SET NULL,
            firmware VARCHAR(64),
            profile VARCHAR(32),
            sha256 VARCHAR(64) NOT NULL,
            size_bytes INTEGER NOT NULL,
            wifi_clients_before INTEGER,
            secret_ciphertext BYTEA NOT NULL,
            dek_wrapped BYTEA NOT NULL,
            kek_id VARCHAR NOT NULL
        )"""))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_onu_config_backup_item "
        "ON onu_config_backup (company_id, inventory_item_id, created_at)"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    count = c.execute(text(
        "SELECT count(*) FROM device_credential WHERE kind = 'CPE_WEB'"
    )).scalar()
    if count:
        raise RuntimeError(
            f"ow1 downgrade refused: {count} CPE_WEB credential(s) exist; "
            "delete them first (the pre-ow1 CHECK rejects the kind)"
        )
    c.execute(text("DROP TABLE IF EXISTS onu_config_backup"))
    c.execute(text("ALTER TABLE provisioning_settings DROP COLUMN IF EXISTS cpe_web_cidrs"))
    _set_kind_check(c, _CREDENTIAL_KIND_CHECK_PRE)
    c.execute(text("ALTER TABLE device_credential DROP COLUMN IF EXISTS priority"))
