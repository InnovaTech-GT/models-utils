"""Inventario: device_category tiers + categories, device_type serialization, inventory_item lots

Revision ID: inv1_general_inventory
Revises: tk2_task_links
Create Date: 2026-09-16

Figma redesign PR 8 (docs/design/plans/08-inventario.md §2, master plan §2.12 /
§4 row 9). The inventory tables already exist — this revision does NOT build a
second product catalog, a stock table or a movements table. It makes the
existing one able to hold things that are not network gear:

1. `device_category.tier` grows CONSUMABLE / TOOL / OTHER. The old fragment
   ("tier IN ('CORE','EDGE')") is frozen byte-for-byte inside
   nc2a_core_config.py — revisions are immutable, so we DROP and RECREATE the
   constraint rather than edit that file. The fragment stays byte-identical to
   the model's (no `tier IS NULL OR` prefix: a bare IN is already NULL-passing,
   and NULL still means passive/unclassified).

2. Nine new system categories, including a real `MUFA` key (master plan Q7,
   RESOLVED 2026-09-15). MUFA is NOT a rename of SPLICE_CLOSURE: `key` is
   immutable, PR 9 needs the Figma-level name, and SPLICE_CLOSURE must keep
   resolving for existing data. Both are passive/NULL-tier and both group under
   "Dispositivos de red". Inserts are ON CONFLICT (key) DO NOTHING — the same
   insert-only covenant the seed keeps, so a super-admin who renamed or
   deactivated a key never has it reverted.

3. Icons backfilled ONLY where `icon IS NULL` — the categories page renders a
   lucide name per row and today most seeded rows have none.

4. `device_type.is_serialized` / `unit`: serialized gear is one row per physical
   unit; consumables are lots. Default TRUE is correct for every existing row.

5. `inventory_item.quantity` / `custodian_user_id` / `label` + `quantity >= 1`
   + two indexes. quantity=1 on every existing row is correct — every item in
   the table today is one serialized unit.

Seed edits ride this revision (MU convention: a seed change ships with a
revision, the prod migrate workflow is path-filtered on alembic/**):
`isp_seed.DEVICE_CATEGORIES` gains the icon column and the nine rows, so a
fresh install and an upgraded install end up identical.

Reversible, with one documented asymmetry: `downgrade()` restores the old
('CORE','EDGE') CHECK, so it first NULLs any tier that only the new vocabulary
allows (CONSUMABLE/TOOL/OTHER). Those rows keep existing and keep their key —
they land in the same "unclassified" bucket passives already live in. The new
category ROWS are deleted only when nothing references them (a device_type FK
is ON DELETE RESTRICT, so the delete is explicitly guarded rather than left to
blow up).

Hand-written (NOT autogenerate), ba1/tk2 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertions, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'inv1_general_inventory'
down_revision: Union[str, Sequence[str], None] = 'tk2_task_links'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors database_utils.models.isp.DEVICE_CATEGORY_TIERS / the CHECK fragment
# built from it (pinned byte-identical by tests/test_general_inventory.py).
DEVICE_CATEGORY_TIERS = ("CORE", "EDGE", "CONSUMABLE", "TOOL", "OTHER")
_DEVICE_CATEGORY_TIER_CHECK = (
    "tier IN ('CORE','EDGE','CONSUMABLE','TOOL','OTHER')"
)
_OLD_DEVICE_CATEGORY_TIER_CHECK = "tier IN ('CORE','EDGE')"
_INVENTORY_QUANTITY_CHECK = "quantity >= 1"

# (key, name, sort_order, tier, is_passive, icon) — the nine new rows of
# isp_seed.DEVICE_CATEGORIES. Duplicated (not imported): revisions are
# immutable, the seed list is not.
_NEW_CATEGORIES = (
    ('MUFA', 'MUFA', 65, None, True, 'box'),
    ('PATCH_CORD', 'Patch cords', 200, 'CONSUMABLE', False, 'cable'),
    ('FIBER_OPTIC', 'Fibra optica', 210, 'CONSUMABLE', False, 'cable'),
    ('DISTRIBUTION_BOX', 'Cajas de distribucion', 220, 'CONSUMABLE', False, 'box'),
    ('MODEM', 'Modems', 230, 'EDGE', False, 'activity'),
    ('SIM_CARD', 'Tarjetas SIM', 240, 'OTHER', False, 'smartphone'),
    ('SET_TOP_BOX', 'Decodificadores', 250, 'EDGE', False, 'gallery-thumbnails'),
    ('FUSION_SPLICER', 'Fusionadora de fibra', 300, 'TOOL', False, 'wrench'),
    ('BARCODE_SCANNER', 'Escaner de codigos', 310, 'TOOL', False, 'scan-qr-code'),
)

# lucide names for the pre-existing rows, applied only where icon IS NULL.
_ICON_BACKFILL = {
    'ROUTER': 'radio-tower',
    'OLT': 'radio',
    'ONU': 'house-wifi',
    'CPE_ROUTER': 'house-wifi',
    'SWITCH': 'network',
    'SPLITTER': 'split',
    'SPLICE_CLOSURE': 'box',
    'ANTENNA': 'antenna',
    'PATCH_PANEL': 'gallery-thumbnails',
    'ACCESS_POINT': 'radio-tower',
    'UPS': 'activity',
    'RADIO': 'radio',
    'OTHER': 'box',
}

_NEW_COLUMNS = (
    ("device_type", "is_serialized"),
    ("device_type", "unit"),
    ("inventory_item", "quantity"),
    ("inventory_item", "custodian_user_id"),
    ("inventory_item", "label"),
)

_NEW_INDEXES = (
    "ix_inventory_item_company_device_type",
    "ix_inventory_item_company_custodian",
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. tier vocabulary -------------------------------------------------
    op.execute(
        "ALTER TABLE device_category DROP CONSTRAINT IF EXISTS ck_device_category_tier"
    )
    op.execute(
        "ALTER TABLE device_category ADD CONSTRAINT ck_device_category_tier CHECK ("
        f"{_DEVICE_CATEGORY_TIER_CHECK})"
    )
    # The generic catch-all category becomes a real tier now that one exists.
    op.execute("UPDATE device_category SET tier = 'OTHER' WHERE key = 'OTHER'")

    # --- 2. new system categories (insert-only) -----------------------------
    for key, name, sort_order, tier, is_passive, icon in _NEW_CATEGORIES:
        connection.execute(
            text(
                "INSERT INTO device_category "
                "(id, key, name, sort_order, tier, is_passive, icon, is_active, is_system, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :key, :name, :sort_order, :tier, :is_passive, :icon, TRUE, TRUE, now(), now()) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {
                "key": key, "name": name, "sort_order": sort_order, "tier": tier,
                "is_passive": is_passive, "icon": icon,
            },
        )

    # --- 3. icon backfill (never overwrites a super-admin's choice) ---------
    for key, icon in _ICON_BACKFILL.items():
        connection.execute(
            text(
                "UPDATE device_category SET icon = :icon "
                "WHERE key = :key AND icon IS NULL"
            ),
            {"key": key, "icon": icon},
        )

    # --- 4. device_type ------------------------------------------------------
    op.execute(
        "ALTER TABLE device_type ADD COLUMN IF NOT EXISTS "
        "is_serialized BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE device_type ADD COLUMN IF NOT EXISTS unit VARCHAR(20)")

    # --- 5. inventory_item ---------------------------------------------------
    op.execute(
        "ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS "
        "quantity INTEGER NOT NULL DEFAULT 1"
    )
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS custodian_user_id UUID")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS label VARCHAR(120)")

    # SET NULL: an offboarded user leaves the item in inventory, unassigned.
    op.execute(
        "ALTER TABLE inventory_item DROP CONSTRAINT IF EXISTS fk_inventory_item_custodian_user_id"
    )
    op.execute(
        "ALTER TABLE inventory_item ADD CONSTRAINT fk_inventory_item_custodian_user_id "
        'FOREIGN KEY (custodian_user_id) REFERENCES "user" (id) ON DELETE SET NULL'
    )

    op.execute(
        "ALTER TABLE inventory_item DROP CONSTRAINT IF EXISTS ck_inventory_item_quantity_positive"
    )
    op.execute(
        "ALTER TABLE inventory_item ADD CONSTRAINT ck_inventory_item_quantity_positive "
        f"CHECK ({_INVENTORY_QUANTITY_CHECK})"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_inventory_item_company_device_type "
        "ON inventory_item (company_id, device_type_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_inventory_item_company_custodian "
        "ON inventory_item (company_id, custodian_user_id)"
    )

    # --- assertions ---------------------------------------------------------
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[inv1] expected column(s) missing after upgrade: {missing}")

    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[inv1] expected index '{index_name}' to exist after upgrade"
            )

    for check_name in ("ck_device_category_tier", "ck_inventory_item_quantity_positive"):
        if connection.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": check_name},
        ).scalar() is None:
            raise RuntimeError(
                f"[inv1] expected constraint '{check_name}' to exist after upgrade"
            )

    absent = connection.execute(
        text(
            "SELECT string_agg(k, ', ') FROM unnest(CAST(:keys AS text[])) AS k "
            "WHERE NOT EXISTS (SELECT 1 FROM device_category dc WHERE dc.key = k)"
        ),
        {"keys": [row[0] for row in _NEW_CATEGORIES]},
    ).scalar()
    if absent:
        raise RuntimeError(f"[inv1] category key(s) missing after upgrade: {absent}")

    print("[inv1_general_inventory] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for index_name in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    op.execute(
        "ALTER TABLE inventory_item DROP CONSTRAINT IF EXISTS ck_inventory_item_quantity_positive"
    )
    # Dropping the columns drops the FK with them.
    for column in ("label", "custodian_user_id", "quantity"):
        op.execute(f"ALTER TABLE inventory_item DROP COLUMN IF EXISTS {column}")

    for column in ("unit", "is_serialized"):
        op.execute(f"ALTER TABLE device_type DROP COLUMN IF EXISTS {column}")

    # Remove the new categories, but ONLY the untouched ones: a row a tenant
    # already built a device_type on is RESTRICT-protected, and a row a
    # super-admin renamed is no longer ours to delete.
    connection.execute(
        text(
            "DELETE FROM device_category dc "
            "WHERE dc.key = ANY(CAST(:keys AS text[])) "
            "AND NOT EXISTS (SELECT 1 FROM device_type dt WHERE dt.category_id = dc.id)"
        ),
        {"keys": [row[0] for row in _NEW_CATEGORIES]},
    )

    # Restore the pre-inv1 vocabulary. Anything still carrying a tier the old
    # CHECK forbids (a surviving new category, or a row a super-admin retagged
    # while the new vocabulary was live) is set back to NULL — the same
    # "unclassified" bucket passives sit in. Data is reclassified, never lost.
    op.execute(
        "UPDATE device_category SET tier = NULL "
        "WHERE tier IS NOT NULL AND tier NOT IN ('CORE','EDGE')"
    )
    op.execute(
        "ALTER TABLE device_category DROP CONSTRAINT IF EXISTS ck_device_category_tier"
    )
    op.execute(
        "ALTER TABLE device_category ADD CONSTRAINT ck_device_category_tier CHECK ("
        f"{_OLD_DEVICE_CATEGORY_TIER_CHECK})"
    )

    print("[inv1_general_inventory] downgrade complete")
