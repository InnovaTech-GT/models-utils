"""ri1 (doc 47 §4.1): inventory_item.received_at — the FIFO key for ONT
auto-assignment — and the receive schemas.

The trap guarded here: `create_item` builds `InventoryItem(**item_in.dict())`,
so a `received_at` key on the create schema would be passed as an explicit
None and written as NULL (the defaults only apply when the key is absent).
"""
import importlib.util
import os
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from database_utils.models.isp import InventoryItem
from database_utils.schemas.inventory import (
    InventoryItemCreate,
    InventoryItemOut,
    InventoryItemUpdate,
    InventoryReceiveIn,
    InventoryReceiveOut,
    InventoryReceiveRow,
)
from database_utils.utils.timezone_utils import GUATEMALA_TZ

_RI1 = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions",
                    "ri1_inventory_received_at.py")


def _load_ri1():
    spec = importlib.util.spec_from_file_location("ri1_under_test", _RI1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_default_fills_received_at(db):
    item = InventoryItem(company_id=uuid.uuid4(), device_type_id=uuid.uuid4(), serial_number="GPON000e2516")
    db.add(item)
    db.flush()
    assert item.received_at is not None


def test_create_schema_dump_does_not_null_received_at(db):
    body = InventoryItemCreate(device_type_id=uuid.uuid4(), serial_number="GPON000e2517")
    item = InventoryItem(company_id=uuid.uuid4(), **body.model_dump())
    db.add(item)
    db.flush()
    assert item.received_at is not None


def test_received_at_only_on_out_schema():
    assert "received_at" not in InventoryItemCreate.model_fields
    assert "received_at" not in InventoryItemUpdate.model_fields
    assert "received_at" in InventoryItemOut.model_fields


def test_out_schema_reads_received_at_from_orm(db):
    item = InventoryItem(company_id=uuid.uuid4(), device_type_id=uuid.uuid4(), serial_number="X1")
    db.add(item)
    db.flush()
    assert InventoryItemOut.model_validate(item).received_at == item.received_at


def test_ri1_chain_position_and_backfill_order():
    ri1 = _load_ri1()
    assert ri1.revision == "ri1_inventory_received_at"
    assert ri1.down_revision == "ta1_task_assignee_model"
    src = open(_RI1).read()
    backfill = src.index("SET received_at = created_at")
    assert backfill < src.index("nullable=False")
    assert 'sa.text("now()")' in src


def _receive(**kw):
    base = {"device_type_id": uuid.uuid4(), "warehouse_id": uuid.uuid4(), "serials": ["A"]}
    return InventoryReceiveIn(**{**base, **kw})


@pytest.mark.parametrize("serials", [[], ["S"] * 101])
def test_receive_in_rejects_bad_batch_size(serials):
    with pytest.raises(ValidationError):
        _receive(serials=serials)


def test_receive_in_accepts_100_serials():
    assert len(_receive(serials=["S"] * 100).serials) == 100


def test_receive_in_rejects_negative_cost():
    with pytest.raises(ValidationError):
        _receive(cost_cents=-1)


def test_receive_in_makes_naive_received_at_guatemala_aware():
    got = _receive(received_at=datetime(2026, 10, 5, 12, 0)).received_at
    assert got.tzinfo is not None
    assert got.utcoffset() == datetime(2026, 10, 5, 12, tzinfo=GUATEMALA_TZ).utcoffset()
    assert _receive().received_at is None


def test_receive_in_caps_supplier_and_reference():
    with pytest.raises(ValidationError):
        _receive(supplier="x" * 121)
    with pytest.raises(ValidationError):
        _receive(reference="x" * 121)


def test_receive_rows_and_out():
    with pytest.raises(ValidationError):
        InventoryReceiveRow(serial_number="A", result="BOGUS")
    row = InventoryReceiveRow(serial_number="A", result="CREATED", warning="SERIAL_PATTERN_MISMATCH")
    out = InventoryReceiveOut(receipt_id=uuid.uuid4(), created=1, warnings=1, duplicates=0, invalid=0, rows=[row])
    assert out.rows[0].item is None
