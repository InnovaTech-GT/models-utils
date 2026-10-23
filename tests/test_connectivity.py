"""core_connectivity_check construction (moved from backend-erp worker.py,
provisioning worker split R1). A gate refusal is the existing
ProvisioningGateError whose errors[0] is byte-identical to the 409 body
backend-erp's enforce_provisioning_gates returned."""
import uuid

import pytest

from database_utils.models.isp import ProvisioningJob, ProvisioningSettings
from database_utils.utils.connectivity import DEFAULT_PROBE_COMMAND, enqueue_connectivity_check
from database_utils.utils.provisioning_gates import ProvisioningGateError


def _enqueue(db, item):
    return enqueue_connectivity_check(db, item, idempotency_key=f"t-{uuid.uuid4()}")


@pytest.mark.parametrize("block,reason", [("kill_switch", "kill_switch"),
                                          ("device_type", "device_type_disabled")])
def test_gate_failure_raises_with_the_old_409_body(db, plant, monkeypatch, block, reason):
    if block == "kill_switch":
        monkeypatch.setenv("PROVISIONING_KILL_SWITCH", "true")
    else:
        plant.types["ROUTER"].provisioning_enabled = False
        db.flush()
    with pytest.raises(ProvisioningGateError) as exc:
        _enqueue(db, plant.core)
    assert exc.value.errors == [{"code": "PROVISIONING_DISABLED", "reason": reason}]
    assert db.query(ProvisioningJob).count() == 0


def test_device_dial_enqueues_ping_plus_cli_probe(db, plant):
    job = _enqueue(db, plant.core)
    assert job.playbook.name == "core_connectivity_check_ssh"
    assert [s["driver"] for s in job.playbook.definition["steps"]] == ["ping", "ssh"]
    assert job.inventory_item_id == plant.core.id
    assert job.variables == {"input.item_id": str(plant.core.id),
                             "input.mgmt_host": plant.core.mgmt_host,
                             "input.probe_command": DEFAULT_PROBE_COMMAND}
    # get-or-create: a second probe reuses the same SYSTEM playbook row
    assert _enqueue(db, plant.core).playbook_id == job.playbook_id


def test_gateway_dial_drops_the_ping_step(db, plant):
    db.add(ProvisioningSettings(company_id=plant.company_id, dial_target="gateway",
                                gateway_host="gw.example"))
    plant.core.cli_protocol = "telnet"
    db.flush()
    job = _enqueue(db, plant.core)
    assert job.playbook.name == "core_connectivity_check_telnet-nat"
    assert [s["driver"] for s in job.playbook.definition["steps"]] == ["telnet"]
