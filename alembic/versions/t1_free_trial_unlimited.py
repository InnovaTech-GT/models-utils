"""Free/Trial tiers: unlimited resource limits + full module set.

Data migration (no schema change). Free-tier limits are lifted until further
notice so that trial/demo companies can exercise every feature; revert by
seeding real limits in a future revision.

Revision ID: t1_free_trial_unlimited
Revises: c8a_playbook_topology
"""
from typing import Sequence, Union

from alembic import op

revision: str = 't1_free_trial_unlimited'
down_revision: Union[str, Sequence[str], None] = 'c8a_playbook_topology'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ALL_MODULES = '["core", "admin", "management", "automations", "inventory", "topologies", "provisioning"]'
UNLIMITED = '{"max_users": -1, "max_products": -1, "max_clients": -1}'


def upgrade() -> None:
    # Merge unlimited limits into the existing features JSON (preserves
    # support/trial_days/features keys) and grant every module. Idempotent.
    op.execute(
        f"""
        UPDATE tier
        SET features = COALESCE(features::jsonb, '{{}}'::jsonb) || '{UNLIMITED}'::jsonb,
            modules  = '{ALL_MODULES}'::json
        WHERE name IN ('Free', 'Trial')
        """
    )


def downgrade() -> None:
    # Restore the pre-lift limits (module lists are left as-is: the previous
    # values were environment-specific and not recoverable from a migration).
    op.execute(
        """
        UPDATE tier
        SET features = COALESCE(features::jsonb, '{}'::jsonb)
                       || '{"max_users": 5, "max_products": 50, "max_clients": 1000}'::jsonb
        WHERE name = 'Free'
        """
    )
    op.execute(
        """
        UPDATE tier
        SET features = COALESCE(features::jsonb, '{}'::jsonb)
                       || '{"max_users": 5, "max_products": 100, "max_clients": 200}'::jsonb
        WHERE name = 'Trial'
        """
    )
