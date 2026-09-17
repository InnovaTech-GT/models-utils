"""Inventario (inv1_general_inventory) guardrails.

Same shape as tests/test_task_links.py: the schema lives in two places that
cannot import each other — the hand-written revision and the SQLAlchemy models
— and `alembic check` is what compares them. Three things here are load-bearing
beyond "the column exists":

  - The tier CHECK fragment. nc2a froze the old ('CORE','EDGE') text forever, so
    inv1 is now the copy that has to stay byte-identical to the model's.
  - The category seed and the revision are hand-duplicated lists. A key seeded
    in one and not the other means fresh installs and upgraded installs
    disagree — and `key` is immutable, so that is not fixable by an edit later.
    MUFA in particular is consumed by PR 9 (master plan Q7).
  - `is_serialized` defaults TRUE and `quantity` defaults 1: every row that
    exists today is one serialized unit, and the API contract (SERIAL_REQUIRED /
    QUANTITY_NOT_ALLOWED) is built on that.
"""
import importlib.util
import os

from sqlalchemy import CheckConstraint

from database_utils.models import isp
from database_utils.models.isp import DeviceType, InventoryItem
from database_utils.schemas.inventory import (
    DeviceTypeCreate,
    DeviceTypeOut,
    DeviceTypeUpdate,
    InventoryItemCreate,
    InventoryItemOut,
    InventoryItemUpdate,
    InventoryProductSummaryOut,
)

_HERE = os.path.dirname(__file__)
_VERSIONS = os.path.join(_HERE, "..", "alembic", "versions")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inv1():
    return _load(
        os.path.join(_VERSIONS, "inv1_general_inventory.py"), "inv1_general_inventory"
    )


def _seed():
    return _load(_ISP_SEED_PATH, "isp_seed_general_inventory_test")


# --- migration chain ---

def test_migration_chain_position():
    inv1 = _inv1()
    assert inv1.revision == "inv1_general_inventory"
    assert inv1.down_revision == "tk2_task_links"


# --- revision <-> model agreement ---

def test_tier_constants_and_fragment_match_the_model():
    inv1 = _inv1()
    assert inv1.DEVICE_CATEGORY_TIERS == isp.DEVICE_CATEGORY_TIERS
    assert inv1._DEVICE_CATEGORY_TIER_CHECK == isp._DEVICE_CATEGORY_TIER_CHECK
    for value in isp.DEVICE_CATEGORY_TIERS:
        assert f"'{value}'" in isp._DEVICE_CATEGORY_TIER_CHECK


def test_new_tiers_are_additive():
    """CORE/EDGE keep their doc-25 meaning and their position."""
    assert isp.DEVICE_CATEGORY_TIERS[:2] == ("CORE", "EDGE")
    assert set(isp.DEVICE_CATEGORY_TIERS[2:]) == {"CONSUMABLE", "TOOL", "OTHER"}


def test_quantity_check_fragment_matches_the_model():
    inv1 = _inv1()
    assert inv1._INVENTORY_QUANTITY_CHECK == isp._INVENTORY_QUANTITY_CHECK


def test_model_declares_the_new_columns_and_constraints():
    dt = DeviceType.__table__.columns
    assert dt["is_serialized"].nullable is False
    assert dt["is_serialized"].server_default.arg == "true"
    assert dt["unit"].nullable is True

    ii = InventoryItem.__table__.columns
    assert ii["quantity"].nullable is False
    assert ii["quantity"].server_default.arg == "1"
    assert ii["label"].nullable is True
    assert ii["custodian_user_id"].nullable is True
    # SET NULL: an offboarded user must not delete inventory.
    fk = next(iter(ii["custodian_user_id"].foreign_keys))
    assert fk.column.table.name == "user"
    assert fk.ondelete == "SET NULL"

    checks = {
        c.name for c in InventoryItem.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_inventory_item_quantity_positive" in checks
    indexes = {i.name for i in InventoryItem.__table__.indexes}
    assert {
        "ix_inventory_item_company_device_type",
        "ix_inventory_item_company_custodian",
    } <= indexes


def test_revision_creates_every_object_the_model_declares():
    body = open(
        os.path.join(_VERSIONS, "inv1_general_inventory.py")
    ).read()
    for name in (
        "is_serialized", "unit", "quantity", "custodian_user_id", "label",
        "ck_inventory_item_quantity_positive",
        "ix_inventory_item_company_device_type",
        "ix_inventory_item_company_custodian",
        "fk_inventory_item_custodian_user_id",
    ):
        assert name in body, f"inv1 never creates {name}"
    assert "SET lock_timeout = '5s'" in body


def test_downgrade_is_total_and_reclassifies_rather_than_fails():
    """The old CHECK forbids the new tiers, so downgrade must clear them first
    — otherwise a DB that used CONSUMABLE/TOOL/OTHER cannot go back at all."""
    body = open(os.path.join(_VERSIONS, "inv1_general_inventory.py")).read()
    downgrade = body.split("def downgrade()", 1)[1]
    assert "tier NOT IN ('CORE','EDGE')" in downgrade
    assert "DROP COLUMN IF EXISTS {column}" in downgrade
    for name in ("quantity", "custodian_user_id", "label", "unit", "is_serialized"):
        assert f'"{name}"' in downgrade, f"downgrade never drops {name}"


# --- category seed invariant ---

def test_revision_and_seed_agree_on_the_new_categories():
    inv1 = _inv1()
    seed_rows = {row[0]: row for row in _seed().DEVICE_CATEGORIES}
    for key, name, sort_order, tier, is_passive, icon in inv1._NEW_CATEGORIES:
        assert key in seed_rows, f"{key} is in the revision but not the seed"
        # seed rows carry a 7th element (is_active, dc1_category_trim) the
        # revision's own literal doesn't know about — compare the shared prefix.
        assert seed_rows[key][:6] == (key, name, sort_order, tier, is_passive, icon)


def test_mufa_is_a_new_passive_key_and_splice_closure_survives():
    """Q7 (master plan §8): MUFA is a NEW key, not a rename — `key` is immutable
    and PR 9 needs the Figma-level name. Both stay passive/NULL tier."""
    seed_rows = {row[0]: row for row in _seed().DEVICE_CATEGORIES}
    assert seed_rows["MUFA"][3] is None and seed_rows["MUFA"][4] is True
    assert seed_rows["SPLICE_CLOSURE"][3] is None
    assert "MUFA" in {row[0] for row in _inv1()._NEW_CATEGORIES}


def test_every_seeded_tier_is_check_legal_and_every_row_has_an_icon():
    for key, _name, _sort, tier, _passive, icon, _active in _seed().DEVICE_CATEGORIES:
        assert tier is None or tier in isp.DEVICE_CATEGORY_TIERS, key
        assert icon, f"{key} has no lucide icon"


def test_icon_backfill_never_disagrees_with_the_seed():
    inv1 = _inv1()
    seed_icons = {row[0]: row[5] for row in _seed().DEVICE_CATEGORIES}
    for key, icon in inv1._ICON_BACKFILL.items():
        assert seed_icons[key] == icon, f"{key}: seed and backfill icons differ"


def test_seed_skips_the_new_vocabulary_before_inv1():
    """The seed runs at every migration position, including after a downgrade,
    where the old CHECK is back. Without the gate the seed crashes."""
    body = open(_ISP_SEED_PATH).read()
    assert "has_general_inventory" in body
    assert "_INV1_CATEGORY_KEYS" in body


# --- schema pins ---

def test_device_type_schemas_carry_serialization():
    created = DeviceTypeCreate(name="Fibra")
    assert created.is_serialized is True and created.unit is None
    assert DeviceTypeCreate(name="Fibra", is_serialized=False, unit="m").unit == "m"
    assert DeviceTypeUpdate().is_serialized is None
    assert {"is_serialized", "unit"} <= set(DeviceTypeOut.model_fields)


def test_inventory_item_create_defaults_to_one_unit():
    item = InventoryItemCreate(device_type_id="11111111-1111-1111-1111-111111111111")
    assert item.quantity == 1
    assert item.label is None and item.custodian_user_id is None
    assert {"client_id", "client_service_id", "cost_cents"} <= set(
        InventoryItemCreate.model_fields
    )


def test_quantity_is_rejected_below_one_on_both_write_schemas():
    import pytest
    for schema, kwargs in (
        (InventoryItemCreate, {"device_type_id": "11111111-1111-1111-1111-111111111111"}),
        (InventoryItemUpdate, {}),
    ):
        with pytest.raises(ValueError):
            schema(quantity=0, **kwargs)


def test_inventory_item_out_carries_the_enrichment_fields():
    fields = set(InventoryItemOut.model_fields)
    assert {
        "updated_at", "quantity", "label", "custodian_user_id", "custodian_name",
        "client_name", "client_address", "warehouse_name", "warehouse_address",
        "parent_id", "parent_label", "network_attached", "location", "acs_state",
        "acs_registration_id", "last_maintenance_at", "cost_cents",
        # pre-existing, the CORE "En linea" source — must not be dropped
        "mgmt_last_check_ok", "mgmt_last_check_at",
    } <= fields
    # Every enrichment is optional so a bare ORM row still serializes.
    assert InventoryItemOut.model_fields["location"].default == "NONE"


def test_product_summary_defaults_to_an_empty_product():
    row = InventoryProductSummaryOut(
        device_type_id="11111111-1111-1111-1111-111111111111",
        name="ONU", category_key="ONU", category_name="ONU / ONT",
    )
    assert row.total == 0 and row.damaged == 0 and row.is_serialized is True
