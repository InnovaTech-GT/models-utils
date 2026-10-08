"""engine v2: run/job phase, run outcome + outputs + secrets, CLI_ENABLE credential kind

Revision ID: pe1_playbook_phases
Revises: tl1_task_location
Create Date: 2026-10-08

ZTP SP1 (doc 42 §12). Additive and metadata-only (nullable columns, no default,
no backfill, no index):

provisioning_run  + phase (CHECK), error_code, error, outputs,
                    secrets_ciphertext, secrets_dek_wrapped, secrets_kek_id
provisioning_job  + phase (CHECK; NULL = standalone or legacy child)
device_credential   ck_device_credential_kind re-created with 'CLI_ENABLE'

The program chain (doc 42a §4) puts this revision after oa1 (tl1 -> oa1 -> pe1);
on tl1 (develop head) until SP4 composes, then re-pointed to oa1 (one line).

The CHECK literals are this revision's own copies (revisions are immutable, so
it cannot import the model); tests/test_pe1_playbook_phases.py pins them
byte-identical to database_utils/models/isp.py. Downgrade refuses while a
CLI_ENABLE credential exists (the old CHECK would reject it).

Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pe1_playbook_phases"
down_revision: Union[str, Sequence[str], None] = "tl1_task_location"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CREDENTIAL_KIND_CHECK = "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST','HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT','CLI_ENABLE')"
_CREDENTIAL_KIND_CHECK_PRE = (
    "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST',"
    "'HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT')"
)
_PROVISIONING_PHASE_CHECK = (
    "phase IS NULL OR phase IN ('PRECONDITIONS','CONFIGURATION','VERIFICATION','ROLLBACK')"
)

_RUN_COLUMNS = (
    ("phase", "VARCHAR(16)"),
    ("error_code", "VARCHAR(40)"),
    ("error", "TEXT"),
    ("outputs", "JSON"),
    ("secrets_ciphertext", "BYTEA"),
    ("secrets_dek_wrapped", "BYTEA"),
    ("secrets_kek_id", "VARCHAR"),
)


def upgrade() -> None:
    c = op.get_bind()
    # The live worker polls provisioning_job every second; never queue behind it.
    c.execute(text("SET lock_timeout = '5s'"))
    for name, sql_type in _RUN_COLUMNS:
        c.execute(text(
            f"ALTER TABLE provisioning_run ADD COLUMN IF NOT EXISTS {name} {sql_type} NULL"
        ))
    c.execute(text(
        "ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS phase VARCHAR(16) NULL"
    ))
    for table in ("provisioning_run", "provisioning_job"):
        c.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_phase"))
        c.execute(text(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_phase "
            f"CHECK ({_PROVISIONING_PHASE_CHECK})"
        ))
    c.execute(text(
        "ALTER TABLE device_credential DROP CONSTRAINT IF EXISTS ck_device_credential_kind"
    ))
    c.execute(text(
        "ALTER TABLE device_credential ADD CONSTRAINT ck_device_credential_kind "
        f"CHECK ({_CREDENTIAL_KIND_CHECK})"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    count = c.execute(text(
        "SELECT count(*) FROM device_credential WHERE kind = 'CLI_ENABLE'"
    )).scalar()
    if count:
        raise RuntimeError(
            f"pe1 downgrade refused: {count} CLI_ENABLE credential(s) exist; "
            "delete them first (the pre-pe1 CHECK rejects the kind)"
        )
    c.execute(text(
        "ALTER TABLE device_credential DROP CONSTRAINT IF EXISTS ck_device_credential_kind"
    ))
    c.execute(text(
        "ALTER TABLE device_credential ADD CONSTRAINT ck_device_credential_kind "
        f"CHECK ({_CREDENTIAL_KIND_CHECK_PRE})"
    ))
    for table in ("provisioning_run", "provisioning_job"):
        c.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_phase"))
    c.execute(text("ALTER TABLE provisioning_job DROP COLUMN IF EXISTS phase"))
    for name, _ in reversed(_RUN_COLUMNS):
        c.execute(text(f"ALTER TABLE provisioning_run DROP COLUMN IF EXISTS {name}"))
