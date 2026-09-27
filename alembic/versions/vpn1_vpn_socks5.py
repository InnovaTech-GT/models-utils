"""vpn mode: per-tenant WireGuard-hub SOCKS5 endpoint

Revision ID: vpn1_vpn_socks5
Revises: iv1_insights_v2
Create Date: 2026-09-16

Originally authored by Mario Cano as `tun1_tunnel_socks5` (mode `tunnel`,
column `tunnel_socks5`). Renamed here: the mode Mario built and lab-validated
IS canon C17's `vpn` — WireGuard plus a SOCKS5 hop — assembled from an
external VPS hub instead of the in-container userspace wireproxy C17 specced.
`tunnel` stays reserved for canon C10's edge agent and stays listed in
backend-erp's `_UNSHIPPED_MODES` (routers/network_access.py), so this rename
frees that name rather than overloading it.

Re-parented off `ng2_provisioning_run_list` (which already had a child) onto
the real single head `iv1_insights_v2`; the original file's docstring claimed
`Revises: pi1_payment_idem`, which disagreed with its own `down_revision` —
that line is corrected, not carried forward.

Unlike nat_zt (gateway_host + nat_port, one shared gateway differentiated by
port), `vpn` dials item.mgmt_host DIRECTLY — the hub has a real kernel route
into the tenant's private network via WireGuard (validated against a live
MikroTik gateway + CPE, 2026-09-15/16), not a single port-mapped gateway.
vpn_socks5 is the proxy hop only; it never replaces mgmt_host the way
gateway_host replaces it under NAT_MODES.

Mirrors nat3_pylon_socks5's shape, with ONE substantive difference: the clamp
is a REAL backfill, not a defensive no-op. `vpn` has been in
NETWORK_ACCESS_MODES and accepted by `POST /network-access/` since nc1a
(_UNSHIPPED_MODES has only ever held 'tunnel'), while resolve_endpoint had no
vpn branch and fell through to the direct return — so a live `vpn` row with no
proxy can exist, and it must be clamped to 'direct' BEFORE any backend
carrying the new schema deploys. NetworkAccessOut inherits
NetworkAccessBase's validator, so such a row would otherwise make
`GET /network-access/` and `GET /network/settings` 500 for the whole tenant.
That is why models-utils migrates first and the backend re-pin follows
(workspace CLAUDE.md pitfall 10) — grep the target DB for `mode='vpn'` first
if you want to know whether the clamp will actually move anything.

downgrade() has the same no-data-loss shape as nat3: dropping this column and
its CHECK loses only the proxy address. A `vpn` tenant on a downgraded schema
fails closed with TRANSPORT_UNAVAILABLE once vpn_socks5 is gone from the
model — correct behaviour for a transport channel with no code path left.
"""
from alembic import op
import sqlalchemy as sa

revision = "vpn1_vpn_socks5"
down_revision = "iv1_insights_v2"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_vpn_transport_constants.py pins them equal.
_NETWORK_ACCESS_VPN_CHECK = "mode != 'vpn' OR vpn_socks5 IS NOT NULL"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.add_column(
        "network_access", sa.Column("vpn_socks5", sa.String(), nullable=True)
    )
    # Same clamp-before-CHECK shape as nat3, but a real backfill here (see the
    # module docstring): 'vpn' has been API-creatable since nc1a.
    op.execute(
        "UPDATE network_access SET mode = 'direct' "
        "WHERE mode = 'vpn' AND vpn_socks5 IS NULL"
    )
    op.create_check_constraint(
        "ck_network_access_vpn_socks5",
        "network_access",
        sa.text(_NETWORK_ACCESS_VPN_CHECK),
    )
    print("[vpn1_vpn_socks5] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.drop_constraint(
        "ck_network_access_vpn_socks5", "network_access", type_="check"
    )
    op.drop_column("network_access", "vpn_socks5")
    print("[vpn1_vpn_socks5] downgrade complete")
