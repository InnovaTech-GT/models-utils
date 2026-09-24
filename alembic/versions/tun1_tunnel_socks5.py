"""tunnel mode: per-tenant WireGuard-hub SOCKS5 endpoint

Revision ID: tun1_tunnel_socks5
Revises: pi1_payment_idem
Create Date: 2026-09-16

Unlike nat_zt (gateway_host + nat_port, one shared gateway differentiated by
port), the `tunnel` mode dials item.mgmt_host DIRECTLY — the hub has a real
kernel route into the tenant's private network via WireGuard (validated
against a live MikroTik gateway + CPE, 2026-09-15/16), not a single
port-mapped gateway. tunnel_socks5 is the proxy hop only; it never replaces
mgmt_host the way gateway_host replaces it under NAT_MODES.

Mirrors nat3_pylon_socks5's shape (additive, no-op for every existing row —
no tenant has ever run `tunnel`, since resolve_endpoint's tunnel branch has
never read anything but mgmt_host until this migration's code-side follow-up).

downgrade() has the same no-data-loss shape as nat3: dropping this column and
its CHECK loses only the proxy address: a `tunnel` tenant on a downgraded
schema fails closed with TRANSPORT_UNAVAILABLE once tunnel_socks5 is gone
from the model — correct behaviour for a transport channel with no code path
left, same reasoning as nat3's downgrade note.
"""
from alembic import op
import sqlalchemy as sa

revision = "tun1_tunnel_socks5"
down_revision = "ng2_provisioning_run_list"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_nat_transport_constants.py (extended) pins
# these equal.
_NETWORK_ACCESS_TUNNEL_CHECK = "mode != 'tunnel' OR tunnel_socks5 IS NOT NULL"


def upgrade() -> None:
    op.add_column(
        "network_access", sa.Column("tunnel_socks5", sa.String(), nullable=True)
    )
    # Same clamp-before-CHECK shape as nat3. No production tenant has ever
    # run `tunnel` (nothing has ever set this column), so this is defensive,
    # not a real backfill.
    op.execute(
        "UPDATE network_access SET mode = 'direct' "
        "WHERE mode = 'tunnel' AND tunnel_socks5 IS NULL"
    )
    op.create_check_constraint(
        "ck_network_access_tunnel_socks5",
        "network_access",
        sa.text(_NETWORK_ACCESS_TUNNEL_CHECK),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_network_access_tunnel_socks5", "network_access", type_="check"
    )
    op.drop_column("network_access", "tunnel_socks5")
