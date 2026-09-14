"""NAT transport: DB-level CHECK tying mode to gateway_host

Revision ID: nat2_gateway_host_check
Revises: nat1_gateway_transport
Create Date: 2026-08-13

Whole-branch review I3: "gateway_host required for NAT mode" was enforced at
exactly one layer (NetworkAccessCreate's Pydantic validator) — an UPDATE that
flips an existing row from 'direct' to 'nat_public' without a gateway_host
skipped it entirely (NetworkAccessUpdate had no cross-field check). This
migration closes it at the layer no caller can bypass.

Defensive per spec: no production tenant has ever run a NAT mode (nat_zt/
nat_public shipped this same cycle), so no existing row should violate the
new CHECK. Scrub-before-constrain anyway, same precedent as nat1's mgmt_port
clamp — a pathological NAT row with no gateway_host is dropped back to
'direct' rather than failing the migration outright.
"""
from alembic import op
import sqlalchemy as sa

revision = "nat2_gateway_host_check"
down_revision = "nat1_gateway_transport"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1
# precedent. tests/test_nat_transport_constants.py pins them equal.
_NETWORK_ACCESS_NAT_GATEWAY_CHECK = (
    "mode NOT IN ('nat_zt','nat_public') OR gateway_host IS NOT NULL"
)


def upgrade() -> None:
    # Same clamp-before-CHECK shape as nat1's mgmt_port fix: any row that
    # would violate the new constraint is scrubbed back to a safe mode first
    # so the ADD CONSTRAINT below cannot fail on pre-existing junk.
    op.execute(
        "UPDATE network_access SET mode = 'direct' "
        "WHERE mode IN ('nat_zt','nat_public') AND gateway_host IS NULL"
    )
    op.create_check_constraint(
        "ck_network_access_nat_gateway_host",
        "network_access",
        sa.text(_NETWORK_ACCESS_NAT_GATEWAY_CHECK),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_network_access_nat_gateway_host", "network_access", type_="check"
    )
