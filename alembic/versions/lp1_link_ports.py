"""link ports: parent_port / uplink_port on inventory_item

Revision ID: lp1_link_ports
Revises: tr1_transport_axis
Create Date: 2026-09-28

Additive only. Two nullable free-text labels on the parent_id edge of the
network graph, plus a partial unique index so one parent port feeds at most
one child. downgrade() loses only the port labels.
"""
from alembic import op
import sqlalchemy as sa

revision = "lp1_link_ports"
down_revision = "tr1_transport_axis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.add_column("inventory_item", sa.Column("parent_port", sa.String(64), nullable=True))
    op.add_column("inventory_item", sa.Column("uplink_port", sa.String(64), nullable=True))
    op.create_index(
        "uq_inventory_item_parent_port",
        "inventory_item",
        ["parent_id", "parent_port"],
        unique=True,
        postgresql_where=sa.text("parent_port IS NOT NULL"),
    )
    print("[lp1_link_ports] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.drop_index("uq_inventory_item_parent_port", table_name="inventory_item")
    op.drop_column("inventory_item", "uplink_port")
    op.drop_column("inventory_item", "parent_port")
    print("[lp1_link_ports] downgrade complete")
