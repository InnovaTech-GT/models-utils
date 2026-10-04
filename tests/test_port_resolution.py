"""Port-level resolution (doc 40 §3.3.1–3.3.2, §3.3.5).

Builds the Cable Santa Rosa shape on the shared `plant`: the two splitters get
their own device types carrying the path roles `mufa_principal` and
`mufa_secundaria`, and ports are linked exactly as the CO0648 backfill leaves
them:

    CORE-1 (router) --unported--> OLT-1 1/4 -> SPL-1 IN
    SPL-1 OUT 6 -> SPL-2 IN;  SPL-2 OUT 4 -> ONT-1 PON
"""

import copy
import uuid
from types import SimpleNamespace

import pytest

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    DeviceType,
    ProvisioningJobStatus,
)
from database_utils.utils.network_graph import assert_links_consistent
from database_utils.utils.playbook_expr import evaluate_all
from database_utils.utils.provisioning_resolution import (
    DEVICE_ATTRIBUTES,
    PORT_ATTRIBUTES,
    ResolutionError,
    _playbook_references_device_variables,
    _playbook_references_token,
    build_device_frame,
    resolve_provisioning,
)
from database_utils.utils.provisioning_runs import create_run

# doc 40 §3.3.4, verbatim.
OLT_DEF = {
    "variables": [],
    "computed": [
        {"key": "onu_id", "expr": "(path.mufa_principal.out_port - 1) * 16 + path.mufa_secundaria.out_port", "min": 1, "max": 128},
        {"key": "svlan", "expr": "500 + device.out_port", "min": 500, "max": 599}],
    "steps": [
        {"name": "Enter Configuration Mode", "driver": "telnet", "timeout_seconds": 30, "template": "enable"},
        {"name": "Authorize ONU", "driver": "telnet", "timeout_seconds": 30,
         "template": "cd gpononu\nset authorization slot {{device.out_slot}} link {{device.out_port | pad_left:2,\"0\"}} type HG260 onuid {{computed.onu_id}} phy_id {{cpe.serial | alnum}}\nset whitelist phy_addr address {{cpe.serial | alnum}} password 1234567890 action add slot {{device.out_slot}} link {{device.out_port | pad_left:2,\"0\"}} onu {{computed.onu_id}} type hg260"},
        {"name": "Add VLAN Tagging", "driver": "telnet", "timeout_seconds": 30,
         "template": "cd epononu\ncd qinq\nset epon slot {{device.out_slot}} pon {{device.out_port | pad_left:2,\"0\"}} onu {{computed.onu_id}} port 1 onuveip 1 33024 {{computed.svlan}} 65535 33024 65535 65535 33024 65535 65535 0 5 65535"}],
    "rollback": []}
ROUTER_DEF = {
    "variables": [],
    "computed": [{"key": "onu_id", "expr": "(path.mufa_principal.out_port - 1) * 16 + path.mufa_secundaria.out_port", "min": 1, "max": 128}],
    "steps": [{"name": "Add Firewall Address List", "driver": "ssh", "timeout_seconds": 30,
               "template": "ip firewall/address-list/add list={{service_plan.address_list | alnum}} address=10.1.{{path.olt.out_port}}.{{computed.onu_id}} comment={{client.id_legacy | alnum | default:\"SIN-ID\"}}-{{client.name | alnum | truncate:48}}"}],
    "rollback": []}
ONU_DEF = {"variables": [], "steps": [{"name": "ONU autorizada vía OLT", "driver": "simulator", "timeout_seconds": 5,
                                       "template": "ONU {{cpe.serial | alnum}} autorizada vía OLT"}], "rollback": []}


class Csr(SimpleNamespace):
    pass


def _role_type(db, plant, role):
    dt = DeviceType(id=uuid.uuid4(), company_id=plant.company_id, name=f"{role} type",
                    category_id=plant.categories["SPLITTER"].id, path_role=role)
    db.add(dt)
    db.flush()
    return dt


@pytest.fixture()
def csr(db, plant):
    principal = _role_type(db, plant, "mufa_principal")
    secundaria = _role_type(db, plant, "mufa_secundaria")
    plant.spl1.device_type_id = principal.id
    plant.spl2.device_type_id = secundaria.id
    plant.spl1.label = "Mufa principal PON 04"
    plant.spl2.label = "Mufa secundaria 04-6"

    ports = SimpleNamespace(
        olt_pon=plant.port(plant.olt, 4, "1/4", slot=1),
        main_in=plant.port(plant.spl1, 0, "IN", direction="UP"),
        main_out6=plant.port(plant.spl1, 6, "OUT 6"),
        main_out7=plant.port(plant.spl1, 7, "OUT 7"),
        sec_in=plant.port(plant.spl2, 0, "IN", direction="UP"),
        sec_out4=plant.port(plant.spl2, 4, "OUT 4"),
        onu_pon=plant.port(plant.cpe, 1, "PON", direction="UP"),
    )
    links = SimpleNamespace(
        main=plant.link(plant.spl1, ports.olt_pon, ports.main_in),
        sec=plant.link(plant.spl2, ports.main_out6, ports.sec_in),
        onu=plant.link(plant.cpe, ports.sec_out4, ports.onu_pon),
    )
    for name, definition in (("olt-activation", OLT_DEF), ("router-activation", ROUTER_DEF),
                             ("onu-activation", ONU_DEF)):
        plant.playbooks[name].definition = copy.deepcopy(definition)
    plant.plan.provisioning_params = [{"key": "address_list", "value": "PLAN_70M_10M"}]
    db.flush()
    db.expire_all()

    client = SimpleNamespace(
        id=uuid.uuid4(), name="MUNICIPALIDAD CHIQUIMULILLA", email="", phone="", address="",
        custom_field_values=[SimpleNamespace(value="CO0648", field_definition=SimpleNamespace(
            field_key="id_legacy", field_type="TEXT"))],
    )
    service = SimpleNamespace(
        id=plant.service.id, company_id=plant.company_id, cpe_item_id=plant.cpe.id,
        client=client, service_plan=plant.plan, provisioning_params=None,
    )
    return Csr(db=db, plant=plant, ports=ports, links=links, service=service)


def _errors(csr, purpose=PURPOSE_ACTIVATION):
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(csr.db, csr.service, purpose)
    return exc.value.errors


def _set_def(csr, name, definition):
    csr.plant.playbooks[name].definition = definition
    csr.db.flush()


def _template(text):
    return {"steps": [{"name": "s", "driver": "simulator", "template": text}]}


# ------------------------------------------------------- out_* emission (§3.3.1)

def test_golden_co0648_ports_and_computed_values(csr):
    """doc 40 §3.3.5: S = 04, T = 6, U = 4 -> onu_id 84, svlan 504."""
    res = resolve_provisioning(csr.db, csr.service)
    assert [(n.out_slot, n.out_port, n.out_port_name) for n in res.path] == [
        (None, None, None),        # ONU (cpe) never has out_*
        (None, 4, "OUT 4"),        # Mufa secundaria
        (None, 6, "OUT 6"),        # Mufa principal
        (1, 4, "1/4"),             # OLT
        (None, None, None),        # router: unported edge
    ]
    olt = next(n for n in res.steps if n.category_key == "OLT")
    router = next(n for n in res.steps if n.category_key == "ROUTER")

    olt_vars = res.shared_variables | res.device_variables[olt.item_id]
    values, missing, errors = evaluate_all(OLT_DEF["computed"], olt_vars)
    assert (values, missing, errors) == (
        {"computed.onu_id": 84, "computed.svlan": 504}, [], [])

    router_vars = res.shared_variables | res.device_variables[router.item_id]
    values, _, _ = evaluate_all(ROUTER_DEF["computed"], router_vars)
    assert values == {"computed.onu_id": 84}
    assert router_vars["path.olt.out_port"] == 4  # 10.1.4.84
    assert res.shared_variables["client.id_legacy"] == "CO0648"


def test_port_keys_are_emitted_only_when_known(csr):
    res = resolve_provisioning(csr.db, csr.service)
    v = res.shared_variables
    assert v["path.olt.out_slot"] == 1
    assert v["path.olt.out_port_name"] == "1/4"
    assert v["path.mufa_secundaria.out_port"] == 4
    # absent, never "" — the renderer's presence test would fail open on ""
    assert "path.mufa_principal.out_slot" not in v
    for attr in PORT_ATTRIBUTES:
        assert f"path.router.{attr}" not in v
        assert f"cpe.{attr}" not in v
    olt = next(n for n in res.steps if n.category_key == "OLT")
    assert res.device_variables[olt.item_id]["device.out_slot"] == 1


def test_frame_keys_are_device_attributes_plus_known_port_attributes(csr):
    res = resolve_provisioning(csr.db, csr.service)
    allowed = set(DEVICE_ATTRIBUTES) | set(PORT_ATTRIBUTES)
    for node in res.path:
        keys = {k.split(".", 1)[1] for k in build_device_frame(node, "x")}
        assert set(DEVICE_ATTRIBUTES) <= keys <= allowed


def test_nodes_carry_label_and_role(csr):
    res = resolve_provisioning(csr.db, csr.service)
    assert res.path[1].label == "Mufa secundaria 04-6"
    assert res.path[1].path_role == "mufa_secundaria"


def test_a_stale_link_is_ignored(csr):
    """A link whose up item is not the node above (a reparent committed between
    the path read and the link read) must not lend that node a port."""
    db, plant = csr.db, csr.plant
    csr.links.main.up_item_id = plant.core.id  # names CORE-1, but SPL-1's parent is OLT-1
    db.flush()
    res = resolve_provisioning(db, csr.service, "SUSPENSION")
    olt = res.path[3]
    assert (olt.out_slot, olt.out_port, olt.out_port_name) == (None, None, None)
    assert "path.olt.out_port" not in res.shared_variables
    with pytest.raises(AssertionError, match="NETWORK_LINK_PARENT_MISMATCH"):
        assert_links_consistent(db)


def test_links_helper_accepts_a_consistent_plant(csr):
    assert_links_consistent(csr.db)


def test_links_helper_catches_a_port_on_another_item(csr):
    csr.links.sec.up_item_id = csr.plant.olt.id
    csr.plant.spl2.parent_id = csr.plant.olt.id
    csr.db.flush()
    with pytest.raises(AssertionError, match="up port belongs to item"):
        assert_links_consistent(csr.db)


# -------------------------------------------------------------------- roles

def test_a_role_held_once_gets_a_frame(csr):
    v = resolve_provisioning(csr.db, csr.service).shared_variables
    assert v["path.mufa_principal.serial"] == "SPL-1"
    assert v["path.mufa_secundaria.serial"] == "SPL-2"
    assert v["path.splitter.serial"] == "SPL-2"  # category keeps nearest-wins


def test_a_repeated_role_is_ambiguous_not_nearest(csr):
    db, plant = csr.db, csr.plant
    plant.spl2.device_type_id = plant.spl1.device_type_id  # both mufa_principal
    db.flush()
    db.expire_all()
    _set_def(csr, "olt-activation", _template("{{cpe.serial}}"))
    _set_def(csr, "router-activation", _template("{{cpe.serial}}"))
    res = resolve_provisioning(db, csr.service)
    assert res.ambiguous_roles == {
        "mufa_principal": [str(plant.spl2.id), str(plant.spl1.id)]}
    assert not any(k.startswith("path.mufa_principal.") for k in res.shared_variables)


def test_reading_an_ambiguous_role_is_role_ambiguous(csr):
    db, plant = csr.db, csr.plant
    plant.spl2.device_type_id = plant.spl1.device_type_id
    db.flush()
    db.expire_all()
    errors = _errors(csr)
    amb = [e for e in errors if e["code"] == "ROLE_AMBIGUOUS"]
    assert amb and amb[0]["role"] == "mufa_principal"
    assert set(amb[0]["item_ids"]) == {str(plant.spl1.id), str(plant.spl2.id)}


def test_a_role_never_overwrites_a_category_frame(csr):
    db, plant = csr.db, csr.plant
    shadow = _role_type(db, plant, "olt")
    plant.spl1.device_type_id = shadow.id
    db.flush()
    db.expire_all()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, csr.service)
    assert exc.value.code == "ROLE_SHADOWS_CATEGORY"
    assert exc.value.errors[0]["role"] == "olt"


# ------------------------------------------------- resolution-time refusal

def test_a_missing_link_is_port_not_recorded_no_link(csr):
    db = csr.db
    db.delete(csr.links.main)
    db.flush()
    errors = _errors(csr)
    olt_errs = [e for e in errors if e["code"] == "PORT_NOT_RECORDED"
                and e["item_id"] == str(csr.plant.olt.id)]
    assert {e["token"] for e in olt_errs} == {
        "device.out_slot", "device.out_port", "path.olt.out_port"}
    assert all(e["reason"] == "no_link" and e["position"] == 3 for e in olt_errs)
    # svlan's operand is missing, so svlan is skipped, not reported twice
    assert not any(e["code"].startswith("COMPUTE_") for e in errors)


def test_a_port_without_slot_is_port_not_recorded_no_slot(csr):
    _set_def(csr, "onu-activation", _template("slot {{path.mufa_principal.out_slot}}"))
    errors = _errors(csr)
    assert errors == [{
        "code": "PORT_NOT_RECORDED", "token": "path.mufa_principal.out_slot",
        "item_id": str(csr.plant.spl1.id), "label": "Mufa principal PON 04",
        "position": 2, "reason": "no_slot", "detail": errors[0]["detail"],
    }]


def test_a_missing_plan_parameter_is_unresolved_before_the_olt_runs(csr):
    csr.plant.plan.provisioning_params = []
    csr.db.flush()
    errors = _errors(csr)
    assert [(e["code"], e["token"], e["reason"]) for e in errors] == [
        ("UNRESOLVED_TOKEN", "service_plan.address_list", "missing_value")]


def test_a_path_segment_nobody_holds_is_not_on_path(csr):
    _set_def(csr, "onu-activation", _template("{{path.switch.serial}}"))
    errors = _errors(csr)
    assert [(e["code"], e["reason"]) for e in errors] == [("UNRESOLVED_TOKEN", "not_on_path")]


def test_default_input_and_bare_tokens_are_not_checked(csr):
    _set_def(csr, "onu-activation", _template(
        '{{ path.switch.serial | default:"none" }} {{path.switch.mac|default :"x"}} '
        "{{input.vlan}} {{vlan}} {{ not a token }}"))
    resolve_provisioning(csr.db, csr.service)


def test_a_literal_containing_default_is_still_checked(csr):
    _set_def(csr, "onu-activation", _template('{{path.switch.serial | append:"default"}}'))
    assert _errors(csr)[0]["code"] == "UNRESOLVED_TOKEN"


def test_rollback_and_on_failure_tokens_are_not_checked(csr):
    definition = _template("{{cpe.serial}}")
    definition["steps"][0]["on_failure"] = [
        {"name": "c", "driver": "simulator", "template": "{{path.switch.serial}}"}]
    definition["rollback"] = [
        {"name": "r", "driver": "simulator", "template": "{{path.switch.serial}}"}]
    _set_def(csr, "onu-activation", definition)
    resolve_provisioning(csr.db, csr.service)


def test_precondition_and_request_tokens_are_checked(csr):
    _set_def(csr, "onu-activation", {"steps": [{
        "name": "s", "driver": "http",
        "request": {"method": "POST", "path": "/x", "body": {"a": "{{path.switch.serial}}"}},
        "precondition": {"template": "{{path.hub.serial}}",
                         "validation": {"expect_contains": "ok"}}}]})
    assert {e["token"] for e in _errors(csr)} == {"path.switch.serial", "path.hub.serial"}


def test_computed_errors_are_fatal(csr):
    definition = copy.deepcopy(OLT_DEF)
    definition["computed"][0]["max"] = 50  # 84 is out of range
    _set_def(csr, "olt-activation", definition)
    errors = _errors(csr)
    assert [(e["code"], e["key"]) for e in errors] == [("COMPUTE_RANGE", "onu_id")]
    assert errors[0]["item_id"] == str(csr.plant.olt.id)


def test_refusal_applies_to_every_purpose(csr):
    _set_def(csr, "onu-suspension", _template("{{path.switch.serial}}"))
    assert _errors(csr, "SUSPENSION")[0]["code"] == "UNRESOLVED_TOKEN"


# ----------------------------------------------------- port-fact drift (PS-1)

_SUSPEND_OLT = {"computed": OLT_DEF["computed"],
                "steps": [{"name": "s", "driver": "telnet",
                           "template": "no onu {{computed.onu_id}} link {{device.out_port}}"}]}


def _activate(csr, dry_run=False, status=ProvisioningJobStatus.SUCCEEDED):
    run = create_run(csr.db, csr.service, PURPOSE_ACTIVATION, dry_run=dry_run)
    run.status = status
    csr.db.flush()
    return run


def _move_onu_to_out7(csr):
    csr.links.sec.up_port_id = csr.ports.main_out7.id
    csr.db.flush()


def test_drift_since_activation_is_refused(csr):
    _set_def(csr, "olt-suspension", _SUSPEND_OLT)
    _activate(csr)
    _move_onu_to_out7(csr)
    errors = _errors(csr, "SUSPENSION")
    assert errors == [{
        "code": "PATH_CHANGED_SINCE_ACTIVATION", "token": "path.mufa_principal.out_port",
        "was": 6, "now": 7, "item_id": str(csr.plant.spl1.id),
        "detail": errors[0]["detail"],
    }]


def test_no_baseline_means_no_drift_check(csr):
    _set_def(csr, "olt-suspension", _SUSPEND_OLT)
    _move_onu_to_out7(csr)
    resolve_provisioning(csr.db, csr.service, "SUSPENSION")


@pytest.mark.parametrize("kwargs", [
    {"dry_run": True},
    {"status": ProvisioningJobStatus.FAILED},
])
def test_dry_or_failed_activations_are_not_a_baseline(csr, kwargs):
    _set_def(csr, "olt-suspension", _SUSPEND_OLT)
    _activate(csr, **kwargs)
    _move_onu_to_out7(csr)
    resolve_provisioning(csr.db, csr.service, "SUSPENSION")


def test_unchanged_ports_and_unread_ports_do_not_drift(csr):
    _set_def(csr, "olt-suspension", _template("no onu {{device.out_port}}"))
    _activate(csr)
    _move_onu_to_out7(csr)  # changes a port nobody in SUSPENSION reads
    resolve_provisioning(csr.db, csr.service, "SUSPENSION")


def test_activation_itself_is_not_drift_checked(csr):
    _activate(csr)
    _move_onu_to_out7(csr)
    resolve_provisioning(csr.db, csr.service, PURPOSE_ACTIVATION)


def test_a_cleared_port_behind_a_default_is_still_drift(csr):
    _set_def(csr, "olt-suspension", _template('no onu {{device.out_port | default:"1"}}'))
    _activate(csr)
    csr.db.delete(csr.links.main)
    csr.db.flush()
    errors = _errors(csr, "SUSPENSION")
    assert [(e["code"], e["token"], e["was"], e["now"]) for e in errors] == [
        ("PATH_CHANGED_SINCE_ACTIVATION", "device.out_port", 4, None)]


# ---------------------------------------------------------- playbook_version

def test_playbook_version_rides_on_nodes_and_plan_entries(csr):
    csr.plant.playbooks["olt-activation"].version = 3
    csr.db.flush()
    run = create_run(csr.db, csr.service, PURPOSE_ACTIVATION)
    assert [p["playbook_version"] for p in run.plan] == [1, 3, 1]
    olt_snapshot = next(p for p in run.path if p["category_key"] == "OLT")
    assert olt_snapshot["playbook_version"] == 3
    assert olt_snapshot["out_port"] == 4
    assert all(p["playbook_version"] is None for p in run.path if p["is_passive"])


# ------------------------------------------------------- fail-open fix (§3.3.2)

def _pb(definition):
    return SimpleNamespace(definition=definition)


def test_a_computed_device_operand_counts_as_device_referencing():
    pb = _pb({"steps": [{"name": "s", "driver": "simulator", "template": "{{computed.x}}"}],
              "computed": [{"key": "x", "expr": "path.olt.out_port + 1"}]})
    assert _playbook_references_device_variables(pb)


def test_a_device_free_computed_block_is_not_device_referencing():
    pb = _pb({"steps": [{"name": "s", "driver": "simulator", "template": "{{computed.x}}"}],
              "computed": [{"key": "x", "expr": "service_plan.vlan + 1"}]})
    assert not _playbook_references_device_variables(pb)


@pytest.mark.parametrize("computed", [
    [{"key": "x", "expr": "1 +"}],
    [{"key": "x"}],
    "not a list",
])
def test_an_unparseable_computed_block_counts_as_referenced(computed):
    pb = _pb({"steps": [{"name": "s", "driver": "simulator", "template": "noop"}],
              "computed": computed})
    assert _playbook_references_device_variables(pb)
    assert _playbook_references_token(pb, "service_plan.pppoe_user")


def test_a_token_read_only_by_a_computed_operand_is_referenced():
    pb = _pb({"steps": [{"name": "s", "driver": "simulator", "template": "{{computed.x}}"}],
              "computed": [{"key": "x", "expr": "service_plan.vlan + 1"}]})
    assert _playbook_references_token(pb, "service_plan.vlan")
    assert not _playbook_references_token(pb, "service_plan.other")
