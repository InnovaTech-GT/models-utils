"""Engine v2 run state machine (doc 42 §5, §6, §7, §8.4, §10.2).

The seeded plant (conftest) is CORE-1 (router) -> OLT-1 -> SPL-1 -> SPL-2 ->
ONT-1; this module rebinds the three ACTIVATION playbooks to v2 definitions
shaped like the CSR ones (doc 42b): the OLT and the router are CLI devices with
preconditions, verification and an undoes-gated rollback; the ONU is one
simulator step with no rollback. Job outcomes are written by hand, the way
backend-erp's executor writes job.log.
"""
import base64
import json
import os
from datetime import timedelta

import pytest
import sqlalchemy as sa

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    PURPOSE_DEPROVISION,
    PURPOSE_REACTIVATION,
    PURPOSE_SUSPENSION,
    DeviceActionLog,
    ProvisioningJob,
    ProvisioningRun,
)
from database_utils.models.isp import (
    ProvisioningJobStatus as S,
)
from database_utils.schemas.playbook import SESSION_PROBE_STEP
from database_utils.utils import provisioning_runs as pr
from database_utils.utils.provisioning_resolution import ResolutionError, ResolvedNode
from database_utils.utils.provisioning_runs import (
    RUN_CLOSED_LISTENERS,
    RunNotRevertible,
    advance_run,
    append_rollback,
    close_run,
    create_or_get_run,
    create_run,
    decrypt_run_secrets,
    order_for_purpose,
    ran_steps,
    repair_stranded_runs,
    retry_rollback,
    revert_run,
)
from database_utils.utils.timezone_utils import now_gt


def _cli(name, driver="telnet", **kw):
    return {"name": name, "label": name.upper(), "driver": driver, "template": f"run {name}"} | kw


OLT = {
    "preconditions": [_cli("onu-visible")],
    "configuration": [_cli("authorize"), _cli("vlan", idempotent=True)],
    "verification": [_cli("power", capture=[{"key": "rx_power", "regex": "(-?\\d+)",
                                             "type": "number", "min": -27, "max": -8}])],
    "rollback": [_cli("unvlan", undoes="vlan"), _cli("unauthorize", undoes="authorize")],
    "outputs": [{"key": "rx_power", "label": "Potencia", "value": "{{capture.rx_power}}",
                 "unit": "dBm", "audience": ["technician", "office"]}],
}
ROUTER = {
    # no preconditions: the session probe is implied (doc 42 §6.4)
    "configuration": [_cli("add", driver="ssh")],
    "verification": [_cli("listed", driver="ssh")],
    "rollback": [_cli("remove", driver="ssh", undoes="add")],
    "outputs": [{"key": "client_ip", "label": "IP", "value": "10.1.4.84",
                 "audience": ["office"]}],
}
ONU = {"configuration": [{"name": "noop", "driver": "simulator", "template": "ok"}]}


@pytest.fixture(autouse=True)
def partial_idem_indexes(db):
    """SQLite builds the two idempotency indexes WITHOUT their Postgres
    predicate; rebuild them partial so a terminal run frees its key, as on PG."""
    for table, name in (("provisioning_run", "uq_provisioning_run_company_idem"),
                        ("provisioning_job", "uq_provisioning_job_company_idem")):
        db.execute(sa.text(f"DROP INDEX {name}"))
        db.execute(sa.text(
            f"CREATE UNIQUE INDEX {name} ON {table} (company_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL AND status IN ('QUEUED','RUNNING','PENDING_INFORM')"))


@pytest.fixture()
def csr(db, plant):
    for key, definition in (("olt", OLT), ("router", ROUTER), ("onu", ONU)):
        plant.playbooks[f"{key}-activation"].definition = definition
    db.flush()
    return plant


def _children(db, run):
    return db.execute(sa.select(ProvisioningJob).where(ProvisioningJob.run_id == run.id)
                      .order_by(ProvisioningJob.run_position)).scalars().all()


def _entry(name, status="SUCCEEDED", stage="command", code="COMMAND_REJECTED", **kw):
    e = {"name": name, "status": status} | kw
    if status == "FAILED":
        e["detail"] = {"stage": stage, "code": code}
        e["code"] = code
    return e


def _settle(db, job, status=S.SUCCEEDED, steps=None, **log):
    job.status = status
    job.finished_at = now_gt()
    names = [s["name"] for s in job_definition_steps(db, job)]
    if steps is None:
        steps = [_entry(n, "SUCCEEDED" if status == S.SUCCEEDED else "FAILED") for n in names]
    job.log = {"steps": steps} | log
    db.flush()
    return advance_run(db, job)


def job_definition_steps(db, job):
    run = db.get(ProvisioningRun, job.run_id)
    return run.plan[job.run_position]["steps"]


def _drive(db, run, fail_at=None, **kw):
    """Succeed every child until position `fail_at`, which fails with kw."""
    job = _children(db, run)[0]
    while job is not None:
        if job.run_position == fail_at:
            return _settle(db, job, kw.pop("status", S.FAILED), **kw)
        job = _settle(db, job)
    return None


def _shape(run):
    return [(e["phase"], e["category_key"]) for e in run.plan]


@pytest.fixture()
def listener():
    calls = []
    fn = lambda db, run: calls.append((run.id, run.status, run.error_code))  # noqa: E731
    RUN_CLOSED_LISTENERS.append(fn)
    yield calls
    RUN_CLOSED_LISTENERS.remove(fn)


@pytest.fixture()
def keks(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_KEKS", json.dumps({"k1": base64.b64encode(os.urandom(32)).decode()}))
    monkeypatch.setenv("CREDENTIALS_ACTIVE_KEK_ID", "k1")


# ------------------------------------------------------------------- ordering

def _nodes(*positions):
    return [ResolvedNode(position=p, item_id=p, serial_number=None, mac_address=None,
                         device_type_id=None, device_type_name=str(p), category_key=None,
                         category_tier=None, mgmt_host=None, mgmt_port=None)
            for p in positions]


@pytest.mark.parametrize("purpose,expected", [
    (PURPOSE_ACTIVATION, [3, 4, 0]),
    (PURPOSE_REACTIVATION, [3, 4, 0]),
    ("CUSTOM_THING", [3, 4, 0]),
    (PURPOSE_SUSPENSION, [0, 4, 3]),
    (PURPOSE_DEPROVISION, [0, 4, 3]),
])
def test_order_for_purpose(purpose, expected):
    assert [n.position for n in order_for_purpose(_nodes(0, 3, 4), purpose)] == expected


def test_order_without_a_configured_cpe():
    assert [n.position for n in order_for_purpose(_nodes(3, 4), PURPOSE_ACTIVATION)] == [3, 4]
    assert [n.position for n in order_for_purpose(_nodes(0), PURPOSE_DEPROVISION)] == [0]


# ------------------------------------------------------------------- the plan

def test_phase_major_build_order_plan(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    assert _shape(run) == [
        ("PRECONDITIONS", "olt"), ("PRECONDITIONS", "router"),
        ("CONFIGURATION", "olt"), ("CONFIGURATION", "router"), ("CONFIGURATION", "onu"),
        ("VERIFICATION", "olt"), ("VERIFICATION", "router"),
    ]
    assert run.phase == "PRECONDITIONS"
    olt_cfg = run.plan[2]
    assert olt_cfg["steps"] == [{"name": "authorize", "label": "AUTHORIZE"},
                                {"name": "vlan", "label": "VLAN"}]
    assert olt_cfg["device_label"] == "OLT model · OLT-1"
    assert olt_cfg["playbook_version"] == csr.playbooks["olt-activation"].version


def test_the_probe_entry(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    router_pre = run.plan[1]
    assert router_pre["probe"] is True
    assert router_pre["steps"][0]["name"] == SESSION_PROBE_STEP
    assert "probe" not in run.plan[0], "the OLT preconditions already start with telnet"


def test_no_probe_for_the_cpe_in_build_order(db, csr):
    csr.playbooks["onu-activation"].definition = {"configuration": [_cli("wifi")]}
    db.flush()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    assert ("PRECONDITIONS", "onu") not in _shape(run)


def test_teardown_order(db, csr):
    run = create_run(db, csr.service, PURPOSE_SUSPENSION)
    assert [e["category_key"] for e in run.plan if e["phase"] == "CONFIGURATION"] == [
        "onu", "router", "olt"], "HG260 -> MikroTik -> OLT"


def test_the_definitions_are_snapshotted(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    pid = str(csr.playbooks["olt-activation"].id)
    assert run.frames["definitions"][pid]["configuration"][0]["name"] == "authorize"
    csr.playbooks["olt-activation"].definition = {"configuration": [_cli("other")]}
    db.flush()
    assert run.frames["definitions"][pid]["configuration"][0]["name"] == "authorize"


def test_legacy_definitions_are_normalized_into_the_snapshot(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert [e["phase"] for e in run.plan] == ["CONFIGURATION"] * 3
    definition = run.frames["definitions"][run.plan[0]["playbook_id"]]
    assert "steps" not in definition and definition["configuration"]


def test_output_key_conflict(db, csr):
    csr.playbooks["onu-activation"].definition = ONU | {"outputs": [
        {"key": "rx_power", "label": "x", "value": "1", "audience": ["office"]}]}
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        create_run(db, csr.service, PURPOSE_ACTIVATION)
    assert exc.value.code == "OUTPUT_KEY_CONFLICT"


def test_children_carry_their_phase_and_step_budget(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.phase == "PRECONDITIONS" and first.max_attempts == 3
    second = _settle(db, first)
    assert second.phase == "PRECONDITIONS" and second.max_attempts == 3  # probe only
    cfg = _settle(db, second)
    assert cfg.phase == "CONFIGURATION" and cfg.max_attempts == 6  # 3 x 2 steps
    assert run.phase == "CONFIGURATION" and run.status == S.RUNNING


# ------------------------------------------------------------------- advance

def test_all_green_succeeds(db, csr, listener):
    csr.service.path_changed_at = now_gt()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run)
    assert run.status == S.SUCCEEDED and run.error_code is None
    assert len(_children(db, run)) == 7
    assert csr.service.path_changed_at is None
    assert listener == [(run.id, S.SUCCEEDED, None)]


def test_a_precondition_failure_ends_the_run_without_rollback(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=0, steps=[_entry("onu-visible", "FAILED",
                                             display="ONU no visible")])
    assert run.status == S.FAILED and run.error_code == "PRECONDITION_FAILED"
    assert run.error == "OLT model · OLT-1 · ONU-VISIBLE: ONU no visible"
    assert all(e["phase"] != "ROLLBACK" for e in run.plan)
    assert len(_children(db, run)) == 1
    assert listener == [(run.id, S.FAILED, "PRECONDITION_FAILED")]


def test_a_cancelled_precondition_cancels_the_run(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=0, status=S.CANCELLED, steps=[])
    assert run.status == S.CANCELLED and run.error_code == "CANCELLED"


def test_a_router_configuration_failure_rolls_back_router_then_olt(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    nxt = _drive(db, run, fail_at=3, steps=[_entry("add", "FAILED", stage="command")])
    assert run.status == S.RUNNING and run.phase == "ROLLBACK"
    assert run.error_code == "CONFIGURATION_FAILED"
    rollback = [e for e in run.plan if e["phase"] == "ROLLBACK"]
    assert [e["category_key"] for e in rollback] == ["router", "olt"], "the CPE never started"
    assert nxt.phase == "ROLLBACK" and nxt.inventory_item_id == csr.core.id
    assert nxt.max_attempts == 5
    assert rollback[0]["ran_steps"] == ["add"]
    # the forward plan is a stable prefix
    assert _shape(run)[:7] == [(e["phase"], e["category_key"]) for e in run.plan[:7]]


@pytest.mark.parametrize("steps", [
    [_entry("authorize", "FAILED", stage="connect", code="CONNECT_TIMEOUT")],
    [_entry("authorize", "SKIPPED"), _entry("vlan", "SKIPPED")],
    [_entry("authorize", "FAILED", stage="render", code="RENDER_FAILED")],
])
def test_nothing_ran_means_failed_with_devices_untouched(db, csr, steps):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=2, steps=steps)
    assert run.status == S.FAILED and run.error_code == "CONFIGURATION_FAILED"
    assert all(e["phase"] != "ROLLBACK" for e in run.plan)


def test_a_crash_on_step_zero_still_rolls_the_olt_back(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=2, steps=[], interrupted_step="authorize")
    rollback = [e for e in run.plan if e["phase"] == "ROLLBACK"]
    assert [e["category_key"] for e in rollback] == ["olt"]
    assert rollback[0]["steps"] == [
        {"name": "unvlan", "label": "UNVLAN", "skip": True},
        {"name": "unauthorize", "label": "UNAUTHORIZE"},
    ], "undoes-gated steps that will not run are pre-marked"


def test_a_verification_failure_rolls_back_every_configured_device(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    nxt = _drive(db, run, fail_at=5, steps=[_entry("power", "FAILED", code="THRESHOLD_VIOLATED")])
    assert run.error_code == "VERIFICATION_FAILED"
    rollback = [e for e in run.plan if e["phase"] == "ROLLBACK"]
    # the ONU (simulator only, no rollback) needs nothing
    assert [e["category_key"] for e in rollback] == ["router", "olt"]
    while nxt is not None:
        nxt = _settle(db, nxt)
    assert run.status == S.ROLLED_BACK and run.error_code == "VERIFICATION_FAILED"
    assert listener == [(run.id, S.ROLLED_BACK, "VERIFICATION_FAILED")]


def test_a_failed_rollback_continues_and_ends_rollback_incomplete(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    rb = _drive(db, run, fail_at=5)
    olt_rb = _settle(db, rb, S.FAILED, steps=[_entry("remove", "FAILED", code="COMMAND_REJECTED",
                                                     label="REMOVE", display="rejected")])
    assert olt_rb is not None, "rollback continues past a failure"
    assert _settle(db, olt_rb) is None
    assert run.status == S.FAILED and run.error_code == "ROLLBACK_INCOMPLETE"
    assert "COMMAND_REJECTED" in run.error and "REMOVE" in run.error
    logs = db.execute(sa.select(DeviceActionLog)).scalars().all()
    assert [log.action for log in logs] == ["rollback_incomplete"]
    assert logs[0].provisioning_job_id == rb.id
    assert listener[-1] == (run.id, S.FAILED, "ROLLBACK_INCOMPLETE")


def test_no_rollback_defined_with_zero_entries_is_rollback_incomplete(db, csr):
    csr.playbooks["olt-activation"].definition = OLT | {"rollback": []}
    csr.playbooks["router-activation"].definition = ROUTER | {"rollback": []}
    db.flush()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=3, steps=[_entry("add", "FAILED")])
    assert run.status == S.FAILED and run.error_code == "ROLLBACK_INCOMPLETE"
    assert "NO_ROLLBACK_DEFINED" in run.error
    assert all(e["phase"] != "ROLLBACK" for e in run.plan)


def test_cancel_during_configuration(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=3, status=S.CANCELLED, steps=[])
    assert run.phase == "ROLLBACK" and run.error_code == "CANCELLED"
    job = _children(db, run)[-1]
    while job is not None:
        job = _settle(db, job)
    assert run.status == S.ROLLED_BACK and run.error_code == "CANCELLED"


def test_cancel_before_anything_ran(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=2, status=S.CANCELLED, steps=[])
    assert run.status == S.CANCELLED and run.error_code == "CANCELLED"


def test_outputs_merge_even_when_the_child_fails(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=5, steps=[_entry("power", "FAILED")],
           outputs=[{"key": "rx_power", "value": "-29.4", "ok": False}])
    assert [(o["key"], o["value"], o["ok"], o["category_key"], o["position"])
            for o in run.outputs] == [("rx_power", "-29.4", False, "olt", 5)]


def test_outputs_last_writer_wins_per_item_and_key(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    job = _children(db, run)[0]
    _settle(db, job, outputs=[{"key": "k", "value": "1"}])
    pr._merge_outputs(run, job, run.plan[0])
    job.log = job.log | {"outputs": [{"key": "k", "value": "2"}]}
    pr._merge_outputs(run, job, run.plan[0])
    assert [o["value"] for o in run.outputs] == ["2"]


def test_legacy_plan_entries_follow_the_old_rule(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    run.plan = [{k: v for k, v in e.items() if k != "phase"} for e in run.plan]
    run.phase = None
    db.flush()
    first = _children(db, run)[0]
    _settle(db, first, S.FAILED, steps=[_entry("onu-visible", "FAILED")])
    assert run.status == S.FAILED and run.error_code is None
    assert len(run.plan) == 7


# ------------------------------------------------------------------- close_run

def test_close_run_skips_listeners_for_dry_runs(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION, dry_run=True)
    close_run(db, run, S.FAILED)
    assert listener == []


def test_a_raising_listener_keeps_the_outcome(db, csr, listener):
    def boom(db, run):
        raise RuntimeError("listener broke")

    RUN_CLOSED_LISTENERS.insert(0, boom)
    try:
        run = create_run(db, csr.service, PURPOSE_ACTIVATION)
        close_run(db, run, S.FAILED, "PRECONDITION_FAILED", "x")
    finally:
        RUN_CLOSED_LISTENERS.remove(boom)
    assert run.status == S.FAILED and run.finished_at is not None
    assert listener == [(run.id, S.FAILED, "PRECONDITION_FAILED")]


# ------------------------------------------------------------------- append / revert / retry

def test_append_rollback_limited_to_items(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run)
    entries = append_rollback(db, run, items=[str(csr.olt.id)])
    assert [e["category_key"] for e in entries] == ["olt"]
    assert run.plan[-1] == entries[0]


def test_ran_steps_reads_the_last_entry_per_name():
    job = ProvisioningJob(log={"steps": [
        _entry(SESSION_PROBE_STEP),
        _entry("a", "FAILED", stage="connect"), _entry("a", "SUCCEEDED"),
        _entry("b", "FAILED", stage="command"),
        _entry("c", "SUCCEEDED"), _entry("c", "FAILED", stage="connect"),
        _entry("d", "SKIPPED"),
    ], "interrupted_step": "e"})
    assert ran_steps(job) == ["a", "b", "e"]


def test_revert_a_succeeded_run(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run)
    first = revert_run(db, run)
    assert run.status == S.RUNNING and run.phase == "ROLLBACK"
    assert run.error_code == "REVERTED" and run.finished_at is None
    assert first.phase == "ROLLBACK" and first.inventory_item_id == csr.core.id
    job = first
    while job is not None:
        job = _settle(db, job)
    assert run.status == S.ROLLED_BACK and run.error_code == "REVERTED"
    assert listener[-1] == (run.id, S.ROLLED_BACK, "REVERTED")


def test_revert_with_nothing_to_roll_back_ends_at_once(db, csr):
    for key in ("olt", "router"):
        csr.playbooks[f"{key}-activation"].definition = {
            "configuration": [{"name": f"{key}-noop", "driver": "simulator", "template": "ok"}]}
    db.flush()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run)
    assert revert_run(db, run) is None
    assert run.status == S.ROLLED_BACK and run.error_code == "REVERTED"


def _refused(db, run, why):
    with pytest.raises(RunNotRevertible) as exc:
        revert_run(db, run)
    assert exc.value.code == "RUN_NOT_REVERTIBLE"
    assert why in exc.value.reason, exc.value.reason


def test_revert_guards(db, csr):
    dry = create_run(db, csr.service, PURPOSE_ACTIVATION, dry_run=True)
    _drive(db, dry)
    _refused(db, dry, "dry run")

    legacy = create_run(db, csr.service, PURPOSE_SUSPENSION)
    legacy.status, legacy.phase = S.SUCCEEDED, None
    _children(db, legacy)[0].status = S.SUCCEEDED
    db.flush()
    _refused(db, legacy, "legacy")

    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _refused(db, run, "SUCCEEDED")
    _drive(db, run)

    later = create_run(db, csr.service, PURPOSE_SUSPENSION)
    _refused(db, run, "in flight")
    later.status = S.SUCCEEDED
    later.created_at = run.created_at + timedelta(seconds=1)
    db.flush()
    _refused(db, run, "later run")


def test_rollback_retry_reruns_only_the_failed_devices(db, csr, listener):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    rb = _drive(db, run, fail_at=5)                       # router rollback first
    olt_rb = _settle(db, rb)                              # router restored
    _settle(db, olt_rb, S.FAILED, steps=[_entry("unauthorize", "FAILED", code="CONNECT_TIMEOUT")])
    assert run.error_code == "ROLLBACK_INCOMPLETE"

    first = retry_rollback(db, run)
    assert run.status == S.RUNNING and run.phase == "ROLLBACK"
    assert run.error_code == "VERIFICATION_FAILED", "the original cause is restored"
    assert first.inventory_item_id == csr.olt.id
    assert _settle(db, first) is None
    assert run.status == S.ROLLED_BACK and run.error_code == "VERIFICATION_FAILED"
    assert [c[1:] for c in listener] == [(S.FAILED, "ROLLBACK_INCOMPLETE"),
                                         (S.ROLLED_BACK, "VERIFICATION_FAILED")]


def test_rollback_retry_guard(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run)
    with pytest.raises(RunNotRevertible):
        retry_rollback(db, run)


def _rollback_incomplete(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    rb = _drive(db, run, fail_at=5)
    olt_rb = _settle(db, rb)
    _settle(db, olt_rb, S.FAILED, steps=[_entry("unauthorize", "FAILED", code="CONNECT_TIMEOUT")])
    assert run.error_code == "ROLLBACK_INCOMPLETE"
    return run


def test_rollback_retry_refuses_while_another_run_for_the_service_is_in_flight(db, csr):
    run = _rollback_incomplete(db, csr)
    create_run(db, csr.service, PURPOSE_SUSPENSION)       # different key, same service
    with pytest.raises(RunNotRevertible) as exc:
        retry_rollback(db, run)
    assert "in flight" in exc.value.reason


def test_rollback_retry_refuses_after_a_later_corrective_run(db, csr):
    run = _rollback_incomplete(db, csr)
    fix = create_run(db, csr.service, PURPOSE_ACTIVATION)  # §7.7 corrective run
    _drive(db, fix)
    assert fix.status == S.SUCCEEDED
    fix.created_at = run.created_at + timedelta(seconds=1)
    db.flush()
    with pytest.raises(RunNotRevertible) as exc:
        retry_rollback(db, run)
    assert "later run" in exc.value.reason


# ------------------------------------------------------------------- secrets

def _wifi_onu():
    return {
        "secrets": [{"key": "wifi_key", "length": 12}],
        "configuration": [{"name": "set-wifi", "driver": "tr069",
                           "request": {"op": "setParameterValues",
                                       "parameters": {"p": ["{{secret.wifi_key}}", "xsd:string"]}}}],
        "rollback": [{"name": "reset", "driver": "tr069", "request": {"op": "factoryReset"},
                      "undoes": "set-wifi"}],
        "outputs": [{"key": "wifi_key", "label": "Clave WiFi", "value": "{{secret.wifi_key}}",
                     "audience": ["technician"], "shareable": True}],
    }


def test_secrets_are_generated_encrypted_and_new_per_run(db, csr, keks):
    csr.playbooks["onu-activation"].definition = _wifi_onu()
    db.flush()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    secrets = decrypt_run_secrets(run)
    assert len(secrets["wifi_key"]) == 12
    assert not set(secrets["wifi_key"]) & set("0O1lI")
    assert secrets["wifi_key"].encode() not in run.secrets_ciphertext
    assert secrets["wifi_key"] not in json.dumps(run.frames)
    _drive(db, run, fail_at=0)
    again, created = create_or_get_run(db, csr.service, PURPOSE_ACTIVATION)
    assert created and decrypt_run_secrets(again)["wifi_key"] != secrets["wifi_key"]


def test_missing_kek_refuses_before_any_child(db, csr, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_KEKS", raising=False)
    csr.playbooks["onu-activation"].definition = _wifi_onu()
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        create_run(db, csr.service, PURPOSE_ACTIVATION)
    assert exc.value.code == "SECRETS_KEY_UNAVAILABLE"
    assert db.execute(sa.select(sa.func.count()).select_from(ProvisioningJob)).scalar() == 0


def test_a_dry_run_generates_nothing(db, csr, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_KEKS", raising=False)
    csr.playbooks["onu-activation"].definition = _wifi_onu()
    db.flush()
    run = create_run(db, csr.service, PURPOSE_ACTIVATION, dry_run=True)
    assert run.secrets_ciphertext is None and decrypt_run_secrets(run) == {}


def test_secret_spec_conflict(db, csr, keks):
    csr.playbooks["onu-activation"].definition = _wifi_onu()
    olt = dict(OLT, secrets=[{"key": "wifi_key", "length": 20}])
    csr.playbooks["olt-activation"].definition = olt
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        create_run(db, csr.service, PURPOSE_ACTIVATION)
    assert exc.value.code == "SECRET_SPEC_CONFLICT"


# ------------------------------------------------------------------- dry run

def test_a_dry_run_walks_every_phase_without_branching(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION, dry_run=True)
    assert _shape(run)[-2:] == [("ROLLBACK", "router"), ("ROLLBACK", "olt")]
    _drive(db, run, fail_at=0, steps=[_entry("onu-visible", "FAILED")])
    job = _children(db, run)[-1]
    assert job.run_position == 1, "a dry run never branches"
    while job is not None:
        job = _settle(db, job)
    assert len(_children(db, run)) == 9
    assert run.status == S.FAILED


def test_a_successful_dry_run_stamps_unchanged_playbooks_only(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION, dry_run=True)
    csr.playbooks["router-activation"].version += 1
    db.flush()
    _drive(db, run)
    assert run.status == S.SUCCEEDED
    olt, router = csr.playbooks["olt-activation"], csr.playbooks["router-activation"]
    assert olt.last_dry_run_version == olt.version
    assert router.last_dry_run_version is None, "edited during the dry run"


# ------------------------------------------------------------------- stranded runs

def _stale(db, run, hours=2):
    for job in _children(db, run):
        job.finished_at = now_gt() - timedelta(hours=hours)
    run.updated_at = now_gt() - timedelta(hours=hours)
    db.flush()


def test_stranded_in_preconditions_expires_failed(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _children(db, run)[0].status = S.SUCCEEDED
    _stale(db, run)
    assert repair_stranded_runs(db) == 1
    assert run.status == S.FAILED and run.error_code == "STRANDED_RUN_EXPIRED"


def test_stranded_in_configuration_enters_rollback(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    job = _children(db, run)[0]
    for _ in range(2):
        job = _settle(db, job)
    job.status = S.SUCCEEDED
    job.log = {"steps": [_entry("authorize"), _entry("vlan")]}
    _stale(db, run)
    assert repair_stranded_runs(db) == 1
    assert run.phase == "ROLLBACK" and run.error_code == "STRANDED_RUN_EXPIRED"
    assert _children(db, run)[-1].phase == "ROLLBACK"


def test_stranded_after_the_last_forward_entry_succeeds_instead_of_rolling_back(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    job = _children(db, run)[0]
    while job.run_position < len(run.plan) - 1:
        job = _settle(db, job)
    job.status, job.finished_at = S.SUCCEEDED, now_gt()   # the final advance never committed
    job.log = {"steps": [_entry(s["name"]) for s in job_definition_steps(db, job)]}
    _stale(db, run)
    assert repair_stranded_runs(db) == 1
    assert run.status == S.SUCCEEDED and run.error_code is None
    assert all(c.phase != "ROLLBACK" for c in _children(db, run))


def test_stranded_in_rollback_keeps_advancing(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    rb = _drive(db, run, fail_at=5)
    rb.status = S.SUCCEEDED
    rb.log = {"steps": [_entry("remove")]}
    _stale(db, run)
    assert repair_stranded_runs(db) == 1
    assert _children(db, run)[-1].inventory_item_id == csr.olt.id
    assert run.status == S.RUNNING


# ------------------------------------------------------------------- re-runs (founder Q10)

@pytest.mark.parametrize("outcome", ["precondition", "rolled_back", "cancelled"])
def test_a_retry_is_a_new_run_on_the_same_key(db, csr, outcome):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    if outcome == "precondition":
        _drive(db, run, fail_at=0)
    elif outcome == "cancelled":
        _drive(db, run, fail_at=0, status=S.CANCELLED, steps=[])
    else:
        job = _drive(db, run, fail_at=5)
        while job is not None:
            job = _settle(db, job)
    assert run.status != S.RUNNING
    again, created = create_or_get_run(db, csr.service, PURPOSE_ACTIVATION)
    assert created and again.id != run.id
    assert again.idempotency_key == run.idempotency_key
    assert _children(db, again)[0].idempotency_key == f"{run.idempotency_key}#0"


def test_no_retry_while_the_previous_run_rolls_back(db, csr):
    run = create_run(db, csr.service, PURPOSE_ACTIVATION)
    _drive(db, run, fail_at=5)
    assert run.phase == "ROLLBACK"
    again, created = create_or_get_run(db, csr.service, PURPOSE_ACTIVATION)
    assert not created and again.id == run.id
