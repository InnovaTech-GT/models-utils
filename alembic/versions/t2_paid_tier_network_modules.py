"""Paid tiers (Basic/Premium/Pro/Enterprise): add Network modules.

Data migration (no schema change). Basic/Premium/Pro/Enterprise never got
`inventory`/`topologies`/`provisioning` added to their `modules` JSON when
those modules were introduced (Cycle 8/10) — only Free/Trial were converged
(t1_free_trial_unlimited). This silently hid the entire Network sidebar
group for every paying tenant. Product decision: all paid tiers get Network.

Revision ID: t2_paid_tier_network_modules
Revises: nat3_pylon_socks5
"""
import json
from typing import Sequence, Union

from alembic import op

revision: str = 't2_paid_tier_network_modules'
down_revision: Union[str, Sequence[str], None] = 'nat3_pylon_socks5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NETWORK_MODULES = json.dumps(["inventory", "topologies", "provisioning"])
PAID_TIER_NAMES = ('Basic', 'Premium', 'Pro', 'Enterprise')


def upgrade() -> None:
    # Union in the missing network modules, don't clobber existing entries.
    # Idempotent.
    op.execute(
        f"""
        UPDATE tier
        SET modules = (
            SELECT COALESCE(jsonb_agg(DISTINCT m), '[]'::jsonb)
            FROM jsonb_array_elements_text(
                COALESCE(modules::jsonb, '[]'::jsonb) || '{NETWORK_MODULES}'::jsonb
            ) AS m
        )
        WHERE name IN {PAID_TIER_NAMES}
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE tier
        SET modules = (
            SELECT COALESCE(jsonb_agg(m), '[]'::jsonb)
            FROM jsonb_array_elements_text(COALESCE(modules::jsonb, '[]'::jsonb)) AS m
            WHERE m NOT IN ('inventory', 'topologies', 'provisioning')
        )
        WHERE name IN {PAID_TIER_NAMES}
        """
    )
