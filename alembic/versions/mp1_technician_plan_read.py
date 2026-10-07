"""Grant service_plans.read to the system TECHNICIAN role

Revision ID: mp1_technician_plan_read
Revises: ld1_legacy_drop
Create Date: 2026-10-03

uplink-mobile tecnicos can create an INSTALL order for a NEW service; its
plan picker reads GET /service-plans, which needs service_plans.read.
Data-only; the seed (isp_seed.ISP_ROLES) was edited in the same commit.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "mp1_technician_plan_read"
down_revision: Union[str, Sequence[str], None] = "ld1_legacy_drop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TECHNICIAN_GRANTS = ("service_plans.read",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for perm in TECHNICIAN_GRANTS:
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = 'TECHNICIAN' AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"perm": perm},
        )


def downgrade() -> None:
    connection = op.get_bind()
    for perm in TECHNICIAN_GRANTS:
        connection.execute(
            text(
                "DELETE FROM role_permission WHERE "
                "role_id IN (SELECT id FROM role WHERE name = 'TECHNICIAN' AND company_id IS NULL) "
                "AND permission_id IN (SELECT id FROM permission WHERE name = :perm)"
            ),
            {"perm": perm},
        )
