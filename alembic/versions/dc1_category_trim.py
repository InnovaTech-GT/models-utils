"""Trim device categories to the six product-backed keys

Revision ID: dc1_category_trim
Revises: fg1_integration_enabled_regby
Create Date: 2026-09-17

USER DECISION: the global device-category list is trimmed to six categories
that every tenant gets as default products (see
utils/inventory_defaults.py in backend-erp): ROUTER, SWITCH, OLT, ONU,
FIBER_OPTIC, PATCH_CORD. The other 16 baseline keys (SPLITTER,
SPLICE_CLOSURE, MUFA, PATCH_PANEL, ACCESS_POINT, CPE_ROUTER, UPS, ANTENNA,
RADIO, OTHER, DISTRIBUTION_BOX, MODEM, SIM_CARD, SET_TOP_BOX,
FUSION_SPLICER, BARCODE_SCANNER) are DEACTIVATED, not deleted:
`device_type.category_id` is a RESTRICT FK and provisioning tasks reference
categories by key, so a hard delete would break existing rows. Deactivated
categories keep their row (key stays a stable identifier) and simply drop
out of `is_active`-filtered pickers/endpoints.

Hand-written (NOT autogenerate), fg1 house style: lock_timeout, idempotent
(plain UPDATE, safe to re-run), post-upgrade assertion, reversible downgrade
(reactivates all 22 keys — the pre-trim state).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'dc1_category_trim'
down_revision: Union[str, Sequence[str], None] = 'fg1_integration_enabled_regby'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_KEYS = ("ROUTER", "SWITCH", "OLT", "ONU", "FIBER_OPTIC", "PATCH_CORD")

_INACTIVE_KEYS = (
    "SPLITTER", "SPLICE_CLOSURE", "MUFA", "PATCH_PANEL", "ACCESS_POINT",
    "CPE_ROUTER", "UPS", "ANTENNA", "RADIO", "OTHER", "DISTRIBUTION_BOX",
    "MODEM", "SIM_CARD", "SET_TOP_BOX", "FUSION_SPLICER", "BARCODE_SCANNER",
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    connection.execute(
        text("UPDATE device_category SET is_active = true WHERE key = ANY(:keys)"),
        {"keys": list(_ACTIVE_KEYS)},
    )
    connection.execute(
        text("UPDATE device_category SET is_active = false WHERE key = ANY(:keys)"),
        {"keys": list(_INACTIVE_KEYS)},
    )

    active_count = connection.execute(
        text("SELECT COUNT(*) FROM device_category WHERE is_active AND key = ANY(:keys)"),
        {"keys": list(_ACTIVE_KEYS)},
    ).scalar()
    if active_count != len(_ACTIVE_KEYS):
        raise RuntimeError(
            f"[dc1] expected {len(_ACTIVE_KEYS)} active categories, found {active_count} "
            "— a baseline key may be missing (check c3b/inv1 seed ran first)"
        )
    stray_active = connection.execute(
        text("SELECT COUNT(*) FROM device_category WHERE is_active AND key = ANY(:keys)"),
        {"keys": list(_INACTIVE_KEYS)},
    ).scalar()
    if stray_active:
        raise RuntimeError(f"[dc1] {stray_active} deactivated categor(y/ies) still active")

    print("[dc1_category_trim] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    connection.execute(text("UPDATE device_category SET is_active = true"))

    print("[dc1_category_trim] downgrade complete")
