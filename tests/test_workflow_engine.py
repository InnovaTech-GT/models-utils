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
