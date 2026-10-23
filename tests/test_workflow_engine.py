"""Unit tests for workflow trigger-condition matching (WF-1).

Focus: `changed_to` / `changed_from` must match only on an actual transition,
not on every subsequent update while the field still holds the target value.
"""
from database_utils.utils.workflow_engine import _matches_field_conditions


def cond(field="status", operator="changed_to", value="CANCELLED"):
    return {"field": field, "operator": operator, "value": value}


def test_none_conditions_match_any():
    assert _matches_field_conditions(None, {"status": "A"}, {"status": "B"}) is True


def test_changed_to_fires_on_transition():
    assert _matches_field_conditions(
        cond(), {"status": "ACTIVE"}, {"status": "CANCELLED"}
    ) is True


def test_changed_to_does_not_refire_when_already_equal():
    # status was already CANCELLED before -> editing an unrelated field must NOT fire.
    assert _matches_field_conditions(
        cond(), {"status": "CANCELLED"}, {"status": "CANCELLED"}
    ) is False


def test_changed_to_on_create_with_no_before_fires_if_equal():
    assert _matches_field_conditions(cond(), None, {"status": "CANCELLED"}) is True


def test_changed_to_false_when_after_not_equal():
    assert _matches_field_conditions(
        cond(), {"status": "ACTIVE"}, {"status": "PAUSED"}
    ) is False


def test_changed_from_fires_on_transition_away():
    assert _matches_field_conditions(
        cond(operator="changed_from", value="ACTIVE"),
        {"status": "ACTIVE"}, {"status": "CANCELLED"},
    ) is True


def test_changed_from_does_not_fire_when_still_equal():
    assert _matches_field_conditions(
        cond(operator="changed_from", value="ACTIVE"),
        {"status": "ACTIVE"}, {"status": "ACTIVE"},
    ) is False


def test_changed_operator_detects_any_change():
    assert _matches_field_conditions(
        cond(operator="changed"), {"status": "A"}, {"status": "B"}
    ) is True
    assert _matches_field_conditions(
        cond(operator="changed"), {"status": "A"}, {"status": "A"}
    ) is False


def test_equals_is_static_state_check():
    assert _matches_field_conditions(
        cond(operator="equals", value="CANCELLED"),
        {"status": "CANCELLED"}, {"status": "CANCELLED"},
    ) is True


def test_explicit_playbook_mode_has_its_imports():
    """Mode B namespaces author variables through input_key.

    That call was added by doc 33 but the import only landed in the OTHER mode,
    so every explicit-playbook enqueue NameError'd from fabaed9 until it was
    found by a dead-code sweep. Compiling the function's module and asserting the
    name resolves is the cheapest thing that fails if it regresses.
    """
    import inspect

    from database_utils.utils import workflow_engine

    src = inspect.getsource(workflow_engine._execute_enqueue_provisioning_explicit)
    assert "input_key(" in src, "mode B should still namespace author variables"
    assert "import input_key" in src, "input_key must be imported inside mode B"


# --- doc 43 §5.6: ENQUEUE_PROVISIONING honours the gates (modes A and B) -----

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from database_utils.models.isp import ProvisioningJob, ProvisioningRun  # noqa: E402
from database_utils.utils.workflow_engine import (  # noqa: E402
    _execute_enqueue_provisioning_explicit,
    _execute_enqueue_provisioning_path,
)


def _rows(db):
    return (db.scalar(sa.select(sa.func.count()).select_from(ProvisioningRun)),
            db.scalar(sa.select(sa.func.count()).select_from(ProvisioningJob)))


def _undry(plant):
    pb = plant.playbooks["olt-activation"]
    pb.version, pb.last_dry_run_version = 2, 1
    plant.db.flush()
    return pb


def _mode_a(db, plant):
    return _execute_enqueue_provisioning_path(
        db, {"use_service_path": True, "client_service_id": str(plant.service.id)},
        plant.company_id)


def _mode_b(db, plant):
    return _execute_enqueue_provisioning_explicit(
        # UUID objects, not strings: SQLite's Uuid bind needs them (PG takes either).
        db, {"playbook_id": plant.playbooks["olt-activation"].id,
             "inventory_item_id": plant.olt.id},
        {}, plant.company_id)


@pytest.mark.parametrize("mode", [_mode_a, _mode_b])
def test_enqueue_with_an_undry_playbook_fails_the_step(db, plant, mode):
    _undry(plant)
    with pytest.raises(ValueError, match="gates blocked.*DRY_RUN_REQUIRED"):
        mode(db, plant)
    assert _rows(db) == (0, 0)


@pytest.mark.parametrize("mode", [_mode_a, _mode_b])
def test_enqueue_under_the_kill_switch_fails_the_step(db, plant, mode, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KILL_SWITCH", "true")
    with pytest.raises(ValueError, match="gates blocked.*kill_switch"):
        mode(db, plant)
    assert _rows(db) == (0, 0)


def test_mode_b_gates_on_the_items_device_type(db, plant):
    plant.types["OLT"].provisioning_enabled = False
    db.flush()
    with pytest.raises(ValueError, match="device_type_disabled"):
        _mode_b(db, plant)


@pytest.mark.parametrize("mode", [_mode_a, _mode_b])
def test_enqueue_passes_when_gates_pass(db, plant, mode):
    assert mode(db, plant)["enqueued"] is True
