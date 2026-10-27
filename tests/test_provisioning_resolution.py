"""Provisioning resolution against the traversed network path (doc 35 §3.2).

Runs against the shared `plant` fixture in conftest.py — a real in-memory
SQLite graph, because the resolver now issues recursive CTEs and binding
lookups that a fake DB could only pretend to answer.
"""

import uuid
from types import SimpleNamespace

import pytest
from conftest import _DEF_DEVICE_FREE, CO_B

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    PURPOSE_CPE_SWAP,
    ProvisioningJobStatus,
)
from database_utils.utils.provisioning_resolution import (
    _DEVICE_VARIABLE_PATTERN,
    _last_activation_frames,
    DEVICE_ATTRIBUTES,
    PORT_ATTRIBUTES,
    ResolutionError,
    build_device_frame,
    iter_client_custom_fields,
    latest_swap_event,
    resolve_playbook_for,
    resolve_provisioning,
)

# --------------------------------------------------------------- path shape

def test_path_is_leaf_to_root(db, plant):
    res = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    assert [n.serial_number for n in res.path] == [
        "ONT-1", "SPL-2", "SPL-1", "OLT-1", "CORE-1"]


def test_passives_are_on_the_path_but_are_not_steps(db, plant):
    res = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    assert [n.category_key for n in res.steps] == ["ONU", "OLT", "ROUTER"]
    assert all(n.is_passive for n in res.path if n.category_key == "SPLITTER")
    assert not any(n.is_passive for n in res.steps)


def test_position_counts_hops_from_the_cpe(db, plant):
    res = resolve_provisioning(db, plant.service)
    assert [n.position for n in res.path] == [0, 1, 2, 3, 4]


# ------------------------------------------------------- playbook resolution

def test_device_type_default_is_used_when_there_is_no_override(db, plant):
    res = resolve_provisioning(db, plant.service)
    olt = next(n for n in res.steps if n.category_key == "OLT")
    assert olt.playbook_source == "device_type"


def test_node_override_beats_the_device_type_default(db, plant):
    special = plant._playbook("olt-special")
    plant.bind_node(plant.olt, PURPOSE_ACTIVATION, special)
    res = resolve_provisioning(db, plant.service)
    olt = next(n for n in res.steps if n.category_key == "OLT")
    assert olt.playbook_id == special.id
    assert olt.playbook_source == "node"


def test_resolve_playbook_for_returns_none_when_nothing_is_bound(db, plant):
    plant.unbind_type(plant.types["OLT"], PURPOSE_ACTIVATION)
    assert resolve_playbook_for(db, plant.olt, PURPOSE_ACTIVATION) == (None, None)


def test_an_active_node_with_no_playbook_is_fatal_for_activation(db, plant):
    plant.unbind_type(plant.types["OLT"], PURPOSE_ACTIVATION)
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    assert exc.value.code == "RESOLUTION_FAILED"
    assert exc.value.errors[0]["code"] == "PLAYBOOK_NOT_BOUND"
    assert exc.value.errors[0]["category"] == "olt"


def test_a_missing_playbook_is_not_fatal_for_a_device_free_suspension(db, plant):
    """A suspend that never templates a device variable must not be blocked by
    an OLT with no suspend playbook."""
    plant.unbind_type(plant.types["OLT"], "SUSPENSION")
    for name in ("router-suspension", "onu-suspension"):
        plant.playbooks[name].definition = _DEF_DEVICE_FREE
    db.flush()
    res = resolve_provisioning(db, plant.service, "SUSPENSION")
    assert [n.category_key for n in res.steps] == ["ONU", "ROUTER"]


def test_a_missing_playbook_is_fatal_for_a_device_reading_suspension(db, plant):
    plant.unbind_type(plant.types["OLT"], "SUSPENSION")
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service, "SUSPENSION")
    assert exc.value.code == "RESOLUTION_FAILED"


def test_an_inactive_playbook_is_never_silently_skipped(db, plant):
    plant.playbooks["olt-activation"].is_active = False
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service)
    assert exc.value.code == "PLAYBOOK_INACTIVE"


def test_a_playbook_owned_by_another_company_is_refused(db, plant):
    plant.playbooks["olt-activation"].company_id = CO_B
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service)
    assert exc.value.code == "PLAYBOOK_INACTIVE"


# ------------------------------------------------------------- preconditions

def test_unset_cpe_is_reported(db, plant):
    plant.service.cpe_item_id = None
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service)
    assert exc.value.code == "CPE_NOT_SET"


def test_detached_cpe_is_reported(db, plant):
    plant.cpe.parent_id = None
    plant.cpe.network_attached = False
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service)
    assert exc.value.code == "CPE_NOT_ATTACHED"


def test_a_cpe_belonging_to_another_company_resolves_to_nothing(db, plant):
    plant.service.company_id = CO_B
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service)
    assert exc.value.code == "CPE_NOT_ATTACHED"


# ----------------------------------------------------------------- variables

def test_shared_variables_expose_the_path_by_category(db, plant):
    v = resolve_provisioning(db, plant.service).shared_variables
    assert v["cpe.serial"] == "ONT-1"
    assert v["path.olt.serial"] == "OLT-1"
    assert v["path.router.serial"] == "CORE-1"
    assert v["path.splitter.serial"] == "SPL-2"
    assert v["cpe.depth"] == 0
    assert v["path.olt.depth"] == 3


def test_path_category_picks_the_node_nearest_the_cpe(db, plant):
    """Two OLTs stacked: the one closest to the subscriber wins, with no
    tie-break needed because the path is already ordered leaf -> root."""
    second = plant._item("OLT-2", "OLT", plant.core)
    plant.olt.parent_id = second.id
    db.flush()
    v = resolve_provisioning(db, plant.service).shared_variables
    assert v["path.olt.serial"] == "OLT-1"


def test_device_frame_is_per_node(db, plant):
    res = resolve_provisioning(db, plant.service)
    olt = next(n for n in res.steps if n.category_key == "OLT")
    assert res.device_variables[olt.item_id]["device.serial"] == "OLT-1"
    assert res.device_variables[plant.cpe.id]["device.serial"] == "ONT-1"


def test_passive_nodes_get_no_device_frame(db, plant):
    res = resolve_provisioning(db, plant.service)
    assert plant.spl1.id not in res.device_variables


def test_no_positional_namespace_survives(db, plant):
    v = resolve_provisioning(db, plant.service).shared_variables
    assert not any(
        k.startswith(("chain[", "edge_devices[", "core_devices[")) for k in v)
    assert not any(k.endswith(".position") for k in v)


def test_plan_fields_are_unchanged(db, plant):
    v = resolve_provisioning(db, plant.service).shared_variables
    assert v["service_plan.name"] == "Fibra 100"
    assert v["service_plan.download_mbps"] == 100
    assert v["service.id"] == str(plant.service.id)


def test_build_device_frame_emits_every_declared_attribute(db, plant):
    """Pin (doc 40 §3.3.1): every DEVICE_ATTRIBUTE always, and nothing beyond
    the PORT_ATTRIBUTES — which appear only when the port is recorded."""
    path = resolve_provisioning(db, plant.service).path
    for node in path:
        frame = set(build_device_frame(node, "cpe"))
        assert {f"cpe.{a}" for a in DEVICE_ATTRIBUTES} <= frame
        assert frame <= {f"cpe.{a}" for a in DEVICE_ATTRIBUTES + PORT_ATTRIBUTES}
    # The seeded plant has no links, so no port key at all.
    assert set(build_device_frame(path[0], "cpe")) == {f"cpe.{a}" for a in DEVICE_ATTRIBUTES}


def test_device_variable_pattern_matches_the_new_namespaces():
    """This pattern FAILS OPEN. A namespace missing from it silently downgrades
    a fatal resolution error into a half-configured customer."""
    for token in ("{{device.serial}}", "{{cpe.serial}}", "{{path.olt.mgmt_host}}",
                  "{{ cpe.serial | upper }}", "{{path.cpe_router.mac}}",
                  "{{ old_cpe.serial }}"):
        assert _DEVICE_VARIABLE_PATTERN.search(token), token
    for token in ("{{chain[1].serial}}", "{{edge_devices[0].serial}}",
                  "{{core_devices[0].serial}}"):
        assert not _DEVICE_VARIABLE_PATTERN.search(token), token


# ------------------------------------------------- old_cpe.* (doc 53 §8.3–8.5)

def test_old_cpe_frame_from_swap_event(db, plant):
    plant.bind_cpe_swap()
    old, ev = plant.swap_event({"swap": True, "door": "OFFICE"})
    assert latest_swap_event(db, plant.service).id == ev.id
    res = resolve_provisioning(db, plant.service, PURPOSE_CPE_SWAP)
    assert res.shared_variables["old_cpe.serial"] == old.serial_number == "ONT-OLD"
    assert res.shared_variables["old_cpe.item_id"] == str(old.id)
    assert res.shared_variables["cpe.serial"] == "ONT-1"
    assert "old_cpe.out_port" not in res.shared_variables
    assert {k for k in res.shared_variables if k.startswith("old_cpe.")} == {
        f"old_cpe.{a}" for a in DEVICE_ATTRIBUTES}


def test_old_cpe_newest_swap_event_wins(db, plant):
    plant.bind_cpe_swap()
    from datetime import timedelta
    _, first = plant.swap_event({"swap": True}, serial="ONT-A")
    newer, second = plant.swap_event({"swap": True}, serial="ONT-B")
    second.created_at = first.created_at + timedelta(seconds=1)
    db.flush()
    res = resolve_provisioning(db, plant.service, PURPOSE_CPE_SWAP)
    assert res.shared_variables["old_cpe.serial"] == "ONT-B"


def _old_cpe_codes(db, plant, **kw):
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service, PURPOSE_CPE_SWAP, **kw)
    assert exc.value.code == "RESOLUTION_FAILED"
    return [e["code"] for e in exc.value.errors]


def test_old_cpe_not_set_live(db, plant):
    plant.bind_cpe_swap()
    assert _old_cpe_codes(db, plant) == ["OLD_CPE_NOT_SET"]


def test_old_cpe_dry_run_stand_in(db, plant):
    plant.bind_cpe_swap()
    res = resolve_provisioning(db, plant.service, PURPOSE_CPE_SWAP, dry_run=True)
    assert res.shared_variables["old_cpe.serial"] == res.shared_variables["cpe.serial"] == "ONT-1"


def test_old_cpe_ignores_non_swap_replaced_event(db, plant):
    """Review Focus 2: a manual REPLACED (no swap flag) must never feed
    old_cpe.* — a CPE_SWAP run would delete a live ONT."""
    plant.bind_cpe_swap()
    plant.swap_event(None)
    plant.swap_event({"swap": False}, serial="ONT-X")
    assert latest_swap_event(db, plant.service) is None
    assert _old_cpe_codes(db, plant) == ["OLD_CPE_NOT_SET"]


def test_old_cpe_ignores_a_swap_of_another_cpe(db, plant):
    """related_item_id must be the service's CURRENT CPE: an older swap's
    event (whose new unit was since swapped out) is not the old unit now."""
    plant.bind_cpe_swap()
    _, ev = plant.swap_event({"swap": True})
    ev.related_item_id = plant.olt.id
    db.flush()
    assert _old_cpe_codes(db, plant) == ["OLD_CPE_NOT_SET"]


def test_unresolved_old_cpe_token_refused_up_front(db, plant):
    plant.bind_cpe_swap()
    plant.swap_event({"swap": True})
    plant.playbooks["onu-cpe_swap"].definition = {"steps": [
        {"name": "s", "driver": "simulator", "template": "rm {{old_cpe.foo}}"}]}
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        resolve_provisioning(db, plant.service, PURPOSE_CPE_SWAP)
    assert exc.value.code == "RESOLUTION_FAILED"
    assert [(e["code"], e.get("token")) for e in exc.value.errors] == [
        ("UNRESOLVED_TOKEN", "old_cpe.foo")]


def test_other_purposes_get_no_old_cpe_frame(db, plant):
    plant.swap_event({"swap": True})
    res = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    assert not [k for k in res.shared_variables if k.startswith("old_cpe.")]


def test_cpe_swap_run_is_drift_baseline(db, plant):
    from datetime import timedelta

    from database_utils.utils.provisioning_runs import create_run
    plant.bind_cpe_swap()
    plant.swap_event({"swap": True})
    act = create_run(db, plant.service, PURPOSE_ACTIVATION)
    swap = create_run(db, plant.service, PURPOSE_CPE_SWAP)
    act.status = swap.status = ProvisioningJobStatus.SUCCEEDED
    swap.created_at = act.created_at + timedelta(seconds=1)
    swap.frames = {"shared": {"marker": "swap"}}
    db.flush()
    assert _last_activation_frames(db, plant.service) == {"shared": {"marker": "swap"}}


# ------------------------------------------- client custom fields (unchanged)

def _detached_service(plant, client):
    """A ClientService stand-in.

    resolve_provisioning reads its inputs with getattr precisely so it survives
    detached and partial objects — the workflow engine hands it exactly this
    shape. Using one here also lets a test vary `client` freely, which the ORM
    relationship would refuse.
    """
    return SimpleNamespace(
        id=plant.service.id,
        company_id=plant.company_id,
        cpe_item_id=plant.cpe.id,
        client=client,
        service_plan=plant.plan,
        provisioning_params=None,
    )


def test_client_custom_fields_are_namespaced_under_client(db, plant):
    client = SimpleNamespace(
        id=uuid.uuid4(), name="Ana", email="a@x.gt", phone="", address="",
        custom_field_values=[SimpleNamespace(
            value="42", field_definition=SimpleNamespace(
                field_key="nodo", field_type="NUMBER"))],
    )
    v = resolve_provisioning(db, _detached_service(plant, client)).shared_variables
    assert v["client.nodo"] == 42


def test_a_builtin_client_field_wins_a_collision(db, plant):
    client = SimpleNamespace(
        id=uuid.uuid4(), name="Ana", email="", phone="", address="",
        custom_field_values=[SimpleNamespace(
            value="shadow", field_definition=SimpleNamespace(
                field_key="name", field_type="TEXT"))],
    )
    v = resolve_provisioning(db, _detached_service(plant, client)).shared_variables
    assert v["client.name"] == "Ana"


def test_an_ungrammatical_custom_key_is_skipped(db):
    client = SimpleNamespace(custom_field_values=[SimpleNamespace(
        value="x", field_definition=SimpleNamespace(
            field_key="5g_profile", field_type="TEXT"))])
    assert list(iter_client_custom_fields(client)) == []


def test_a_missing_client_relationship_does_not_crash(db, plant):
    v = resolve_provisioning(db, _detached_service(plant, None)).shared_variables
    assert "client.id" not in v


def test_client_code_is_a_builtin_variable_and_wins_a_collision(db, plant):
    # cc1: client.code is built in; a tenant custom field keyed `code` must not shadow it.
    client = SimpleNamespace(
        id=uuid.uuid4(), name="Ana", email="", phone="", address="", code="CO0648",
        custom_field_values=[SimpleNamespace(
            value="shadow", field_definition=SimpleNamespace(
                field_key="code", field_type="TEXT"))],
    )
    v = resolve_provisioning(db, _detached_service(plant, client)).shared_variables
    assert v["client.code"] == "CO0648"
