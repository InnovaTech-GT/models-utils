"""Provisioning gates enforced at run creation (doc 43 §5.6): the gate logic
moved from backend-erp's utils/provisioning_guards.py, keeping today's 409
bodies, plus the gate in create_run that covers every producer."""
import uuid

import pytest
import sqlalchemy as sa

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    ProvisioningJob,
    ProvisioningRun,
)
from database_utils.utils import provisioning_gates as g
from database_utils.utils.provisioning_gates import ProvisioningGateError
from database_utils.utils.provisioning_resolution import resolve_provisioning
from database_utils.utils.provisioning_runs import create_or_get_run, create_run


@pytest.fixture()
def kill_switch(monkeypatch):
    monkeypatch.setenv("PROVISIONING_KILL_SWITCH", "true")


def _undry(plant, key="olt-activation"):
    pb = plant.playbooks[key]
    pb.version, pb.last_dry_run_version = 3, 2
    plant.db.flush()
    return pb


def test_kill_switch_env(monkeypatch):
    for value, expected in (("true", True), ("1", True), (" YES ", True), ("false", False), ("", False)):
        monkeypatch.setenv("PROVISIONING_KILL_SWITCH", value)
        assert g.kill_switch_enabled() is expected
    monkeypatch.delenv("PROVISIONING_KILL_SWITCH")
    assert g.kill_switch_enabled() is False


def test_gate_failure_bodies(db, plant, monkeypatch):
    pb, dt = plant.playbooks["olt-activation"], plant.types["OLT"]
    co = plant.company_id
    assert g.gate_failure(db, co, pb, dt, dry_run=False) is None

    _undry(plant)
    assert g.gate_failure(db, co, pb, dt, dry_run=False) == {
        "code": "DRY_RUN_REQUIRED", "playbook_version": 3, "last_dry_run_version": 2}
    assert g.gate_failure(db, co, pb, dt, dry_run=True) is None

    dt.provisioning_enabled = False
    assert g.gate_failure(db, co, pb, dt, dry_run=False) == {
        "code": "PROVISIONING_DISABLED", "reason": "device_type_disabled"}
    assert g.enable_gate_reason(db, co, dt) == "device_type_disabled"

    monkeypatch.setenv("PROVISIONING_KILL_SWITCH", "true")
    assert g.gate_failure(db, co, pb, dt, dry_run=False) == {
        "code": "PROVISIONING_DISABLED", "reason": "kill_switch"}
    assert g.enable_gate_reason(db, co, None) == "kill_switch"


def test_system_playbooks_are_exempt_from_the_dry_run_gate(db, plant):
    pb = _undry(plant)
    for name in ("cpe_reboot", "core_connectivity_check_ssh-nat"):
        pb.name = name
        assert name in g.SYSTEM_PLAYBOOK_NAMES
        assert g.gate_failure(db, plant.company_id, pb, None, dry_run=False) is None
    assert g.CORE_CONNECTIVITY_PLAYBOOK_NAMES <= g.SYSTEM_PLAYBOOK_NAMES


def test_run_gate_failures_lists_every_blocked_node(db, plant):
    resolved = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    assert g.run_gate_failures(db, resolved, dry_run=False) == []
    olt = _undry(plant)
    plant.types["ONU"].provisioning_enabled = False
    db.flush()
    failures = g.run_gate_failures(db, resolved, dry_run=False)
    by_item = {f["item_id"]: f for f in failures}
    assert len(failures) == 2
    assert by_item[str(plant.olt.id)] == {
        "code": "DRY_RUN_REQUIRED", "playbook_version": 3, "last_dry_run_version": 2,
        "item_id": str(plant.olt.id), "playbook_id": str(olt.id)}
    assert by_item[str(plant.cpe.id)]["reason"] == "device_type_disabled"
    assert g.run_gate_failures(db, resolved, dry_run=True) == []


def test_run_gate_failures_refuses_a_vanished_playbook(db, plant):
    """A playbook deleted after resolution (or a stale `resolution=`) is a
    structured refusal, not an AttributeError."""
    resolved = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    node = resolved.steps[0]
    node.playbook_id = uuid.uuid4()
    failures = g.run_gate_failures(db, resolved, dry_run=False)
    assert failures == [{"code": "PLAYBOOK_NOT_FOUND", "item_id": str(node.item_id),
                         "playbook_id": str(node.playbook_id)}]


def _counts(db):
    return (db.scalar(sa.select(sa.func.count()).select_from(ProvisioningRun)),
            db.scalar(sa.select(sa.func.count()).select_from(ProvisioningJob)))


def test_create_run_raises_when_live_and_never_when_dry(db, plant):
    _undry(plant)
    with pytest.raises(ProvisioningGateError) as e:
        create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert [f["code"] for f in e.value.errors] == ["DRY_RUN_REQUIRED"]
    assert _counts(db) == (0, 0)
    assert create_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True).dry_run is True


def test_create_or_get_run_propagates_the_gate_and_keeps_the_session(db, plant, kill_switch):
    with pytest.raises(ProvisioningGateError) as e:
        create_or_get_run(db, plant.service, PURPOSE_ACTIVATION)
    assert {f["reason"] for f in e.value.errors} == {"kill_switch"}
    assert _counts(db) == (0, 0)
    run, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True)
    assert created and run.dry_run
