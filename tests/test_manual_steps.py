"""Manual playbook steps (doc 42d §4, §7, §9, §15 models-utils): the
`driver: manual` schema and its save-time codes, the warnings, the plan kind,
PENDING_MANUAL in the in-flight set (pinned to the three partial indexes and to
zm1), the ZTP_MANUAL_STEP kind, and the run helpers that must treat a parked
manual child as live and a parked/timed-out manual entry as "ran"."""
import re
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models import USER_NOTIFICATION_KINDS, UserNotification, auth
from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    ProvisioningJob,
    ProvisioningJobStatus,
    ProvisioningRun,
)
from database_utils.schemas.playbook import (
    MANUAL_TIMEOUT_DEFAULT,
    MANUAL_TIMEOUT_MAX,
    MANUAL_TIMEOUT_MIN,
    PlaybookDefinition,
    PlaybookStep,
    is_resend_safe,
    playbook_warnings,
)
from database_utils.utils import provisioning_runs
from database_utils.utils.provisioning_resolution import _step_tokens
from database_utils.utils.provisioning_runs import (
    IN_FLIGHT,
    _step_labels,
    create_run,
    ran_steps,
    repair_stranded_runs,
)
from database_utils.utils.timezone_utils import now_gt


def _manual(name="configure-onu", **kw):
    spec = kw.pop("manual", None) or {"instructions": "Configure la ONU {{cpe.serial}}"}
    return {"name": name, "driver": "manual", "manual": spec} | kw


def _sim(name, **kw):
    return {"name": name, "driver": "simulator", "template": "x"} | kw


def _def(cfg=None, **kw):
    return {"configuration": cfg or [_manual()]} | kw


def _ok(definition):
    return PlaybookDefinition.model_validate(definition)


def _err(definition, code):
    with pytest.raises(ValidationError) as exc:
        PlaybookDefinition.model_validate(definition)
    assert code in str(exc.value), str(exc.value)


def _codes(warnings):
    return [w["code"] for w in warnings]


# ------------------------------------------------------------------- schema

def test_csr_shape_round_trips():
    d = _ok(_def(
        [_manual(label="Configurar la ONU", hint="Revise la WAN", timeout_seconds=1800, manual={
            "instructions": "1. VLAN {{computed.svlan}}\n2. URL {{acs.url}}",
            "fields": [
                {"key": "vlan", "label": "VLAN", "value": "{{computed.svlan}}"},
                {"key": "acs_pass", "label": "Contraseña TR-069",
                 "value": "{{acs.inform_password}}", "secret": True},
                {"key": "inform", "label": "Informe (s)", "value": "60", "copyable": False},
            ],
            "checklist": [{"key": "wan", "label": "WAN creada"}],
        })],
        computed=[{"key": "svlan", "expr": "500 + 1"}],
        verification=[_sim("acs-inform")],
        rollback=[_manual("onu-config-notice", undoes="configure-onu",
                          manual={"instructions": "Deje la ONU configurada"})],
    ))
    dumped = d.model_dump()
    again = PlaybookDefinition.model_validate(dumped).model_dump()
    assert again == dumped
    step = dumped["configuration"][0]
    assert step["manual"]["fields"][0] == {"key": "vlan", "label": "VLAN",
                                           "value": "{{computed.svlan}}",
                                           "copyable": True, "secret": False}
    assert step["manual"]["fields"][1]["secret"] is True
    assert dumped["rollback"][0]["manual"]["fields"] == []


def test_manual_timeout_default_and_range():
    assert (MANUAL_TIMEOUT_DEFAULT, MANUAL_TIMEOUT_MIN, MANUAL_TIMEOUT_MAX) == (1800, 300, 3600)
    assert PlaybookStep.model_validate(_manual()).timeout_seconds == 1800
    assert PlaybookStep.model_validate(_manual(timeout_seconds=300)).timeout_seconds == 300
    assert PlaybookStep.model_validate(_manual(timeout_seconds=3600)).timeout_seconds == 3600
    for bad in (30, 299, 3601):
        with pytest.raises(ValidationError, match="timeout_seconds must be 300-3600"):
            PlaybookStep.model_validate(_manual(timeout_seconds=bad))
    # other drivers keep their own range
    with pytest.raises(ValidationError, match="1-600"):
        PlaybookStep.model_validate(_sim("s", timeout_seconds=1800))


def test_manual_phase_not_allowed():
    _ok(_def())
    _ok(_def([_sim("c")], rollback=[_manual("notice")]))
    _err(_def([_sim("c")], preconditions=[_manual("p")]), "MANUAL_PHASE_NOT_ALLOWED")
    _err(_def([_sim("c")], verification=[_manual("v")]), "MANUAL_PHASE_NOT_ALLOWED")


def test_manual_spec_required():
    with pytest.raises(ValidationError, match="MANUAL_SPEC_REQUIRED"):
        PlaybookStep.model_validate({"name": "m", "driver": "manual"})
    with pytest.raises(ValidationError, match="MANUAL_SPEC_REQUIRED"):
        PlaybookStep.model_validate(_manual(manual={"instructions": "   "}))
    with pytest.raises(ValidationError, match="2000"):
        PlaybookStep.model_validate(_manual(manual={"instructions": "x" * 2001}))


@pytest.mark.parametrize("extra", [
    {"template": "x"}, {"request": {"op": "x"}},
    {"validation": {"expect_contains": "ok"}},
    {"precondition": {"template": "x", "validation": {"expect_contains": "y"}}},
    {"idempotent": True}, {"config_mode": True},
    {"capture": [{"key": "c", "regex": "(x)"}]},
    {"wait_until": {"tries": 2, "interval_seconds": 1}},
    {"target_item_id": "{{device.item_id}}"},
])
def test_manual_field_not_allowed(extra):
    with pytest.raises(ValidationError, match="MANUAL_FIELD_NOT_ALLOWED"):
        PlaybookStep.model_validate(_manual(**extra))


def test_manual_block_on_another_driver_is_refused():
    with pytest.raises(ValidationError, match="MANUAL_FIELD_NOT_ALLOWED"):
        PlaybookStep.model_validate(_sim("s", manual={"instructions": "x"}))


def test_rollback_manual_is_a_notice():
    notice = {"instructions": "Deje la ONU"}
    _ok(_def(rollback=[_manual("n", manual=notice)]))
    _err(_def(rollback=[_manual("n", manual=notice | {
        "fields": [{"key": "a", "label": "A", "value": "1"}]})]), "MANUAL_FIELD_NOT_ALLOWED")
    _err(_def(rollback=[_manual("n", manual=notice | {
        "checklist": [{"key": "a", "label": "A"}]})]), "MANUAL_FIELD_NOT_ALLOWED")


def test_manual_secret_in_text():
    for text in ("Clave {{acs.inform_password}}", "Clave {{secret.wifi_key}}",
                 "{{input.admin_password}}"):
        with pytest.raises(ValidationError, match="MANUAL_SECRET_IN_TEXT"):
            PlaybookStep.model_validate(_manual(manual={"instructions": text}))
    PlaybookStep.model_validate(_manual(manual={"instructions": "URL {{acs.url}}"}))


def test_checklist_and_field_labels_are_static():
    for spec in ({"instructions": "x", "checklist": [{"key": "a", "label": "{{cpe.serial}}"}]},
                 {"instructions": "x", "fields": [{"key": "a", "label": "{{cpe.serial}}",
                                                   "value": "1"}]}):
        with pytest.raises(ValidationError, match="static"):
            PlaybookStep.model_validate(_manual(manual=spec))


def test_secret_field_is_forced_and_exactly_one_token():
    f = PlaybookStep.model_validate(_manual(manual={"instructions": "x", "fields": [
        {"key": "p", "label": "P", "value": "{{acs.inform_password}}"},
        {"key": "w", "label": "W", "value": "{{ secret.wifi_key }}"},
    ]})).manual.fields
    assert [x.secret for x in f] == [True, True]
    for value in ("x{{acs.inform_password}}", "{{acs.inform_password | upper}}",
                  "{{secret.a}}{{secret.b}}"):
        with pytest.raises(ValidationError, match="MANUAL_SECRET_MIXED"):
            PlaybookStep.model_validate(_manual(manual={"instructions": "x", "fields": [
                {"key": "p", "label": "P", "value": value}]}))
    with pytest.raises(ValidationError, match="MANUAL_SECRET_MIXED"):
        PlaybookStep.model_validate(_manual(manual={"instructions": "x", "fields": [
            {"key": "p", "label": "P", "value": "{{acs.inform_password}}", "secret": False}]}))
    # a hand-marked secret field is held to the one-token rule (the reveal renders one lookup)
    assert PlaybookStep.model_validate(_manual(manual={"instructions": "x", "fields": [
        {"key": "p", "label": "P", "value": "{{computed.svlan}}", "secret": True}]})).manual.fields[0].secret
    for value in ("hunter2", "VLAN {{computed.svlan}} x", "{{computed.svlan | upper}}"):
        with pytest.raises(ValidationError, match="MANUAL_SECRET_MIXED"):
            PlaybookStep.model_validate(_manual(manual={"instructions": "x", "fields": [
                {"key": "p", "label": "P", "value": value, "secret": True}]}))


def test_field_and_checklist_keys_and_limits():
    def spec(**kw):
        return _manual(manual={"instructions": "x"} | kw)
    for bad in ({"fields": [{"key": "A", "label": "A", "value": "1"}]},
                {"fields": [{"key": "a", "label": "A", "value": "1"}] * 2},
                {"fields": [{"key": f"f{i}", "label": "A", "value": "1"} for i in range(13)]},
                {"fields": [{"key": "a", "label": "A", "value": "x" * 513}]},
                {"checklist": [{"key": "a", "label": "A"}] * 2},
                {"checklist": [{"key": f"c{i}", "label": "A"} for i in range(9)]},
                {"checklist": [{"key": "a", "label": "x" * 121}]}):
        with pytest.raises(ValidationError):
            PlaybookStep.model_validate(spec(**bad))
    PlaybookStep.model_validate(spec(
        fields=[{"key": f"f{i}", "label": "A", "value": "1"} for i in range(12)],
        checklist=[{"key": f"c{i}", "label": "A"} for i in range(8)]))


def test_token_checks_scan_the_manual_block():
    _err(_def([_manual(manual={"instructions": "{{capture.rx}}"})]), "CAPTURE_UNDECLARED")
    _err(_def([_manual(manual={"instructions": "x", "fields": [
        {"key": "w", "label": "W", "value": "{{secret.wifi_key}}"}]})]), "SECRET_UNDECLARED")
    _err(_def([_manual(manual={"instructions": "{{computed.svlan}}"})]), "COMPUTE_NAME")
    _ok(_def([_sim("read", capture=[{"key": "rx", "regex": "(x)"}]),
              _manual(manual={"instructions": "{{capture.rx}}", "fields": [
                  {"key": "w", "label": "W", "value": "{{secret.wifi_key}}"}]})],
             secrets=[{"key": "wifi_key"}]))


def test_resolver_scans_the_manual_block():
    """The resolver's up-front refusal must see manual tokens (fail-open rule)."""
    tokens, _ = _step_tokens(_def([_manual(manual={
        "instructions": "{{path.olt.out_port}}",
        "fields": [{"key": "s", "label": "S", "value": "{{cpe.serial}}"}]})]))
    names = {name for name, _ in tokens}
    assert {"path.olt.out_port", "cpe.serial"} <= names


def test_is_resend_safe_manual():
    assert is_resend_safe(_manual())


def test_warnings():
    d = _def(rollback=[_manual("n", undoes="configure-onu")])
    assert "MANUAL_UNVERIFIED" in _codes(playbook_warnings(d))
    assert "MANUAL_UNVERIFIED" not in _codes(playbook_warnings(d | {
        "verification": [_sim("v")]}))
    assert "NOT_RESEND_SAFE" not in _codes(playbook_warnings(d))
    assert "MANUAL_OUTSIDE_ACTIVATION" in _codes(playbook_warnings(d, purpose="SUSPENSION"))
    assert "MANUAL_OUTSIDE_ACTIVATION" not in _codes(playbook_warnings(d, purpose="ACTIVATION"))
    assert "MANUAL_OUTSIDE_ACTIVATION" not in _codes(playbook_warnings(d))
    # a person changed the device: no rollback = ROLLBACK_EMPTY
    assert "ROLLBACK_EMPTY" in _codes(playbook_warnings(_def()))
    assert "ROLLBACK_EMPTY" not in _codes(playbook_warnings(d))


def test_step_labels_kind():
    assert _step_labels([_manual(label="Configurar"), _sim("s")]) == [
        {"name": "configure-onu", "label": "Configurar", "kind": "manual"},
        {"name": "s", "label": "s"},
    ]


# --------------------------------------------------- in-flight set and zm1

IN_FLIGHT_SQL = "status IN ('QUEUED','RUNNING','PENDING_INFORM','PENDING_MANUAL')"


def zm1():
    return load("versions/zm1_manual_step.py", "zm1_manual_step")


def _where(table, name):
    idx = next(i for i in table.indexes if i.name == name)
    return str(idx.dialect_options["postgresql"]["where"])


def test_in_flight_matches_the_three_index_predicates():
    assert ProvisioningJobStatus.PENDING_MANUAL.value == "PENDING_MANUAL"
    assert ProvisioningJobStatus.PENDING_MANUAL in IN_FLIGHT
    literal = "status IN (" + ",".join(f"'{s.value}'" for s in IN_FLIGHT) + ")"
    assert literal == IN_FLIGHT_SQL == zm1()._IN_FLIGHT
    for table, name in ((ProvisioningJob.__table__, "uq_provisioning_job_company_idem"),
                        (ProvisioningJob.__table__, "uq_provisioning_job_device_lock"),
                        (ProvisioningRun.__table__, "uq_provisioning_run_company_idem")):
        where = _where(table, name)
        assert re.sub(r"\s+", " ", where).endswith(IN_FLIGHT_SQL), where


def test_zm1_chain_and_literals():
    m = zm1()
    assert m.revision == "zm1_manual_step" and len(m.revision) <= 32
    assert m.down_revision == "zt1_ztp_trigger"
    assert m._IN_FLIGHT_PRE == "status IN ('QUEUED','RUNNING','PENDING_INFORM')"
    assert m._KIND_CHECK == auth._USER_NOTIFICATION_KIND_CHECK
    zt1 = load("versions/zt1_ztp_trigger.py", "zt1_ztp_trigger")
    assert m._KIND_CHECK_PRE == zt1._KIND_CHECK


def test_manual_step_notification_kind(db):
    assert USER_NOTIFICATION_KINDS[-1] == "ZTP_MANUAL_STEP"
    assert "'ZTP_MANUAL_STEP'" in auth._USER_NOTIFICATION_KIND_CHECK
    db.add(UserNotification(company_id=uuid.uuid4(), user_id=uuid.uuid4(),
                            kind="ZTP_MANUAL_STEP", dedupe_key="ztp_manual_step:j:s",
                            push_state="PENDING"))
    db.flush()


# --------------------------------------------------------------- run helpers

def _children(db, run):
    return db.execute(sa.select(ProvisioningJob).where(ProvisioningJob.run_id == run.id)
                      .order_by(ProvisioningJob.run_position)).scalars().all()


def test_repair_stranded_skips_a_run_parked_on_a_manual_step(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    child = _children(db, run)[0]
    child.status = ProvisioningJobStatus.PENDING_MANUAL
    before = run.status
    run.updated_at = now_gt() - timedelta(minutes=50)
    db.flush()
    assert repair_stranded_runs(db) == 0
    assert run.status == before and run.finished_at is None
    assert [j.run_position for j in _children(db, run)] == [0]
    assert provisioning_runs.find_in_flight_run(db, plant.company_id, run.idempotency_key) == run


def test_ran_steps_counts_a_parked_or_timed_out_manual_entry():
    parked = {"name": "configure-onu", "status": "PENDING",
              "detail": {"stage": "command", "kind": "manual"}}
    timed_out = {"name": "configure-onu", "status": "FAILED", "code": "MANUAL_TIMEOUT",
                 "detail": {"stage": "command"}}
    for entry in (parked, timed_out):
        job = ProvisioningJob(log={"steps": [entry]})
        assert ran_steps(job) == ["configure-onu"]
