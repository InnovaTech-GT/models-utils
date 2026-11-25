"""Inventory schema hardening (security audit #69, #70, schema half of #12/#13).

probe_command is a read-only verb (it runs on core devices every sweep), the
`swap` metadata key is reserved for backend-erp's CPE swap, and the management
fields that pick a dial target are one shared list for the backend's
stronger-permission gate."""
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from database_utils.schemas.inventory import (
    INVENTORY_MGMT_FIELDS,
    PROBE_COMMAND_VERBS,
    DeviceTypeCreate,
    DeviceTypeOut,
    DeviceTypeUpdate,
    EquipmentEventBase,
    EquipmentEventCreate,
    EquipmentEventOut,
    InventoryItemUpdate,
    is_safe_probe_command,
)
from database_utils.utils.connectivity import (
    DEFAULT_PROBE_COMMAND,
    enqueue_connectivity_check,
)


@pytest.mark.parametrize("cmd", ["display version", "show version", "SHOW running-config",
                                 "/system resource print", "get sysinfo", "/interface print"])
def test_safe_probe_commands(cmd):
    assert is_safe_probe_command(cmd)


@pytest.mark.parametrize("cmd", ["reboot", "display version\nreboot", "/system reboot", "x" * 101, "",
                                 "undo ont 0 1", "show ver\x1b", "/system reboot; /interface print",
                                 "/ip print where name=[/system reboot]", "/interface print detail", "  ", None, 5])
def test_unsafe_probe_commands(cmd):
    assert not is_safe_probe_command(cmd)


def test_verbs_constant():
    assert PROBE_COMMAND_VERBS == ("show", "display", "get")


def test_device_type_create_and_update_refuse_unsafe_probe():
    with pytest.raises(ValidationError, match="PROBE_COMMAND_UNSAFE"):
        DeviceTypeUpdate(default_attributes={"probe_command": "reboot"})
    with pytest.raises(ValidationError, match="PROBE_COMMAND_UNSAFE"):
        DeviceTypeCreate(name="x", default_attributes={"probe_command": "reboot"})
    assert DeviceTypeUpdate(default_attributes={"probe_command": "show version", "x": 1})
    assert DeviceTypeUpdate(default_attributes={"other": "reboot"})


def test_device_type_out_still_reads_a_stored_unsafe_probe():
    out = DeviceTypeOut.model_validate({
        "id": uuid.uuid4(), "company_id": uuid.uuid4(), "name": "x",
        "default_attributes": {"probe_command": "reboot"},
        "category_id": uuid.uuid4(), "created_at": datetime(2026, 1, 1),
    })
    assert out.default_attributes["probe_command"] == "reboot"


def test_stored_unsafe_probe_falls_back(db, plant):
    plant.types["ROUTER"].default_attributes = {"probe_command": "reboot"}
    db.flush()
    job = enqueue_connectivity_check(db, plant.core, idempotency_key=f"t-{uuid.uuid4()}")
    assert job.variables["input.probe_command"] == DEFAULT_PROBE_COMMAND


def test_stored_safe_probe_is_used(db, plant):
    plant.types["ROUTER"].default_attributes = {"probe_command": "/system resource print"}
    db.flush()
    job = enqueue_connectivity_check(db, plant.core, idempotency_key=f"t-{uuid.uuid4()}")
    assert job.variables["input.probe_command"] == "/system resource print"


def test_event_create_refuses_swap_flag():
    with pytest.raises(ValidationError, match="RESERVED_METADATA_KEY"):
        EquipmentEventCreate(event_type="REPLACED", event_metadata={"swap": True})
    assert EquipmentEventCreate(event_type="REPLACED", event_metadata={"note": 1})


def test_event_out_still_reads_swap_events():
    out = EquipmentEventOut.model_validate({
        "id": uuid.uuid4(), "company_id": uuid.uuid4(), "item_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 1), "event_type": "REPLACED",
        "event_metadata": {"swap": True},
    })
    assert out.event_metadata["swap"] is True
    assert issubclass(EquipmentEventOut, EquipmentEventBase)
    assert not issubclass(EquipmentEventOut, EquipmentEventCreate)


def test_mgmt_fields_constant_matches_update_schema():
    assert INVENTORY_MGMT_FIELDS == frozenset({"mgmt_host", "mgmt_port", "cli_protocol", "nat_port"})
    assert INVENTORY_MGMT_FIELDS <= set(InventoryItemUpdate.model_fields)
