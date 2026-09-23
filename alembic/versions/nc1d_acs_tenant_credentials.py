"""network_access: tenant-scoped TR-069 inform credentials (username + bcrypt hash)

Revision ID: nc1d_acs_tenant_credentials
Revises: iv1_insights_v2
Create Date: 2026-09-23

Plan 23 F1.0/F1.1, as decided with Ricardo (2026-09-23): each tenant's ACS
transport row (network_access, kind='acs') carries the TR-069 inform
credentials its CPEs present to our ACS over HTTP Basic auth. Unlike
device_credential (secrets WE use to dial devices, envelope-encrypted per
canon C1/C19), this credential is presented BY the device TO us, so only a
bcrypt hash is stored: backend-erp compares it, never reads it back.

1. network_access.acs_username String NULL, globally UNIQUE: the username a
   CPE presents is what identifies its tenant. NULLs are distinct, so rows
   without credentials never collide.
2. network_access.acs_password_hash String NULL (bcrypt, via
   database_utils.utils.password).
3. CHECK: only kind='acs' rows may carry credentials.

Purely additive: both columns start NULL on every existing row, so the CHECK
holds without any scrub (contrast nat2/nat3). backend-erp issues credentials
later.

Independent of nc1c_cwmp_inform_credentials (per-device credentials, branch
feature/capa3-fault-isolation, not in develop): the name only follows the
plan-23 phase-1 numbering.

downgrade() drops both constraints and both columns. The only data lost is
issued credentials, and a downgraded backend has no code path that reads them.
"""
import sqlalchemy as sa

from alembic import op

revision = "nc1d_acs_tenant_credentials"
down_revision = "iv1_insights_v2"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_network_access_acs_credentials.py pins them equal.
_NETWORK_ACCESS_ACS_CREDENTIALS_CHECK = (
    "kind = 'acs' OR (acs_username IS NULL AND acs_password_hash IS NULL)"
)


def upgrade() -> None:
    op.add_column(
        "network_access", sa.Column("acs_username", sa.String(), nullable=True)
    )
    op.add_column(
        "network_access", sa.Column("acs_password_hash", sa.String(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_network_access_acs_username", "network_access", ["acs_username"]
    )
    op.create_check_constraint(
        "ck_network_access_acs_credentials_kind",
        "network_access",
        sa.text(_NETWORK_ACCESS_ACS_CREDENTIALS_CHECK),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_network_access_acs_credentials_kind", "network_access", type_="check"
    )
    op.drop_constraint(
        "uq_network_access_acs_username", "network_access", type_="unique"
    )
    op.drop_column("network_access", "acs_password_hash")
    op.drop_column("network_access", "acs_username")
