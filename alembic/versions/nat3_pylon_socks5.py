"""nat_zt redesign: per-tenant Pylon SOCKS5 endpoint

Revision ID: nat3_pylon_socks5
Revises: nat2_gateway_host_check
Create Date: 2026-08-17

Doc 34 OV17 (2026-08-17): one Pylon `refract` process joins exactly one
ZeroTier network — a shared fleet Pylon cannot serve nat_zt for more than one
tenant. The SOCKS5 endpoint moves from a worker env var (PYLON_SOCKS5, spec
N4 — retracted) to network_access.pylon_socks5, mirroring gateway_host.

Defensive per the nat1/nat2 precedent: no production tenant has ever run
nat_zt (resolve_endpoint has fail-closed on it since nat1 shipped, because
PYLON_SOCKS5 was never set), so nothing should need scrubbing. Scrub anyway —
a pathological nat_zt row with no proxy is dropped back to 'direct' rather
than failing the migration outright.

downgrade() has no data-loss ambiguity (contrast nat1's mode downgrade, which
had to choose between an UPDATE and raising): dropping this column and its
CHECK loses only the proxy address and leaves `mode` untouched. A nat_zt
tenant on a downgraded schema fails closed with TRANSPORT_UNAVAILABLE, which
is the correct behaviour for a transport channel with no code path left.
"""
from alembic import op
import sqlalchemy as sa

revision = "nat3_pylon_socks5"
down_revision = "nat2_gateway_host_check"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2 precedent. tests/test_nat_transport_constants.py pins them equal.
_NETWORK_ACCESS_PYLON_CHECK = "mode != 'nat_zt' OR pylon_socks5 IS NOT NULL"


def upgrade() -> None:
    op.add_column(
        "network_access", sa.Column("pylon_socks5", sa.String(), nullable=True)
    )
    # Same clamp-before-CHECK shape as nat2.
    op.execute(
        "UPDATE network_access SET mode = 'direct' "
        "WHERE mode = 'nat_zt' AND pylon_socks5 IS NULL"
    )
    op.create_check_constraint(
        "ck_network_access_pylon_socks5",
        "network_access",
        sa.text(_NETWORK_ACCESS_PYLON_CHECK),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_network_access_pylon_socks5", "network_access", type_="check"
    )
    op.drop_column("network_access", "pylon_socks5")
