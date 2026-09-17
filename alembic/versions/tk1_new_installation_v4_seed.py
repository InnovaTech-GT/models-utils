"""new-installation template v4 seed (task-context cycle, doc 32).

No-op schema revision: the seed change (installation fee param moves from the
retired legacy product to a SERVICE PLAN — param type service_plan, item key
service_plan_id) ships with this revision so the prod migration workflow
replays seeds. Fresh tenants could never satisfy the old required product
UUID (products have no create path anymore); installed v3 tenant copies keep
running (product_id deprecated-but-honored during the rollback window).

Revision ID: tk1_new_installation_v4
Revises: rb1_recurrente_billing
"""
from typing import Sequence, Union

revision: str = 'tk1_new_installation_v4'
down_revision: Union[str, Sequence[str], None] = 'rb1_recurrente_billing'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema no-op — seeds run after upgrade via alembic/env.py.
    pass


def downgrade() -> None:
    pass
