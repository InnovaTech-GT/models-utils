"""NAT transport mode: gateway port-mapping as a second reachability path

Revision ID: nat1_gateway_transport
Revises: ng2_topology_drop
Create Date: 2026-08-13

Spec: docs/superpowers/specs/2026-08-13-nat-transport-mode-design.md §8

Purely additive. Every existing row is untouched: no tenant is switched to a
NAT mode by this migration, and Cable Santa Rosa (the live production tenant)
keeps whatever mode it already has.

DOWNGRADE POLICY — deliberate choice, per spec §8. downgrade() RAISES if any
network_access row is in a NAT mode. Silently rewriting those rows to 'direct'
would leave the tenant's gateway_host and every device's nat_port stranded in
columns this downgrade then drops, and the next upgrade would come back with
mode='direct' pointing at a management LAN nothing can reach. Losing data is
not a downgrade. Switch the tenants off NAT explicitly first.
"""
from alembic import op
import sqlalchemy as sa

revision = "nat1_gateway_transport"
down_revision = "ng2_topology_drop"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nc2a
# precedent. tests/test_nat_transport_constants.py pins them equal.
_NETWORK_ACCESS_MODE_CHECK = "mode IN ('direct','vpn','tunnel','nat_zt','nat_public')"
_NAT_PORT_CHECK = "nat_port IS NULL OR (nat_port BETWEEN 1 AND 65535)"
_MGMT_PORT_CHECK = "mgmt_port IS NULL OR (mgmt_port BETWEEN 1 AND 65535)"

_OLD_MODE_CHECK = "mode IN ('direct','vpn','tunnel')"


def upgrade() -> None:
    op.add_column("network_access", sa.Column("gateway_host", sa.String(), nullable=True))
    op.add_column("inventory_item", sa.Column("nat_port", sa.Integer(), nullable=True))
    op.add_column("inventory_item", sa.Column("mgmt_host_key", sa.String(), nullable=True))

    op.drop_constraint("ck_network_access_mode", "network_access", type_="check")
    op.create_check_constraint(
        "ck_network_access_mode", "network_access", sa.text(_NETWORK_ACCESS_MODE_CHECK)
    )

    # mgmt_port has had no range CHECK since nc2a. Any pre-existing junk would
    # make the ALTER fail, so clamp first — NULL means "driver default", which
    # is the correct reading of a nonsense port.
    op.execute(
        "UPDATE inventory_item SET mgmt_port = NULL "
        "WHERE mgmt_port IS NOT NULL AND (mgmt_port < 1 OR mgmt_port > 65535)"
    )
    op.create_check_constraint(
        "ck_inventory_item_nat_port", "inventory_item", sa.text(_NAT_PORT_CHECK)
    )
    op.create_check_constraint(
        "ck_inventory_item_mgmt_port", "inventory_item", sa.text(_MGMT_PORT_CHECK)
    )
    op.create_index(
        "uq_inventory_item_company_nat_port",
        "inventory_item",
        ["company_id", "nat_port"],
        unique=True,
        postgresql_where=sa.text("nat_port IS NOT NULL"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    stuck = conn.execute(
        sa.text(
            "SELECT count(*) FROM network_access WHERE mode IN ('nat_zt','nat_public')"
        )
    ).scalar()
    if stuck:
        raise RuntimeError(
            f"refusing to downgrade: {stuck} network_access row(s) are in a NAT mode. "
            "Switch those tenants to another mode first — see the module docstring."
        )

    op.drop_index("uq_inventory_item_company_nat_port", table_name="inventory_item")
    op.drop_constraint("ck_inventory_item_mgmt_port", "inventory_item", type_="check")
    op.drop_constraint("ck_inventory_item_nat_port", "inventory_item", type_="check")
    op.drop_constraint("ck_network_access_mode", "network_access", type_="check")
    op.create_check_constraint(
        "ck_network_access_mode", "network_access", sa.text(_OLD_MODE_CHECK)
    )
    op.drop_column("inventory_item", "mgmt_host_key")
    op.drop_column("inventory_item", "nat_port")
    op.drop_column("network_access", "gateway_host")
