"""Revoke device_credentials.read from the system VIEWER role

Revision ID: vw1_viewer_no_credential_read
Revises: cc1_client_code
Create Date: 2026-10-05

rr1_four_builtin_roles gave VIEWER every `*.read` permission. Founder decision
(v1.0.0 release plan, D2): read-only users must not see device credentials,
not even their metadata. VIEWER keeps every other read.
Data-only; the seed filter (rbac_seed.VIEWER_PERMISSION_FILTER) was edited in
the same commit so the post-upgrade seed does not re-grant it.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "vw1_viewer_no_credential_read"
down_revision: Union[str, Sequence[str], None] = "cc1_client_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEWER_REVOKED = ("device_credentials.read",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for perm in VIEWER_REVOKED:
        connection.execute(
            text(
                "DELETE FROM role_permission WHERE "
                "role_id IN (SELECT id FROM role WHERE name = 'VIEWER' AND company_id IS NULL) "
                "AND permission_id IN (SELECT id FROM permission WHERE name = :perm)"
            ),
            {"perm": perm},
        )


def downgrade() -> None:
    connection = op.get_bind()
    for perm in VIEWER_REVOKED:
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = 'VIEWER' AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"perm": perm},
        )
