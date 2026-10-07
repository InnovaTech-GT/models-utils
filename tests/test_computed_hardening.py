"""Security review F1-F3 (ADR-006 integration): computed-block hardening."""
import pytest
from pydantic import ValidationError

from database_utils.schemas.playbook import PlaybookDefinition
from database_utils.utils.playbook_expr import evaluate_all
from database_utils.utils.provisioning_resolution import _has_default_filter, _step_tokens

DECL = [{"key": "onu", "expr": "1 + 1"}]


def _definition(template):
    return {"variables": [], "computed": DECL,
            "steps": [{"name": "s", "driver": "simulator", "template": template}]}


# --- F1: computed tokens must be exactly {{computed.<key>}} (+ filters) -----
@pytest.mark.parametrize("template", [
    "x {{computed.onu.y}}", "x {{computed[0].x}}", "x {{ computed.onu.y | trim }}",
])
def test_f1_extra_segments_are_refused_at_save(template):
    with pytest.raises(ValidationError, match="COMPUTE_NAME"):
        PlaybookDefinition.model_validate(_definition(template))


@pytest.mark.parametrize("template", [
    "x {{computed.onu}}", "x {{ computed.onu | pad_left:3,\"0\" }}", "x {{computed.onu}}{{client.name}}",
])
def test_f1_well_formed_tokens_still_save(template):
    PlaybookDefinition.model_validate(_definition(template))


# --- F2: a quoted filter argument never reads as `| default:` --------------
def test_f2_quoted_argument_is_not_a_default_filter():
    assert not _has_default_filter('path.olt.out_port | replace:"|default:","x"')
    assert not _has_default_filter("path.olt.out_port | replace:'| default :','x'")
    assert _has_default_filter('path.olt.out_port | default:"0"')
    assert _has_default_filter('path.olt.out_port | replace:"a","b" | default:"0"')


def test_f2_step_tokens_do_not_skip_a_disguised_default():
    tokens, _malformed = _step_tokens(_definition('{{path.olt.out_port | replace:"|default:","x"}}'))
    assert ("path.olt.out_port", False) in tokens


# --- F3: malformed stored blocks fail closed with an error, never raise ------
@pytest.mark.parametrize("computed", [{"a": 1}, "x"])
def test_f3_non_list_block_is_a_syntax_error(computed):
    values, missing, errors = evaluate_all(computed, {})
    assert values == {} and missing == []
    assert errors[0]["code"] == "COMPUTE_SYNTAX"


@pytest.mark.parametrize("entry", ["x", 7, {"key": 1, "expr": "1"}, {"key": "a"}])
def test_f3_malformed_entry_is_a_syntax_error(entry):
    _values, _missing, errors = evaluate_all([entry], {})
    assert errors and errors[0]["code"] == "COMPUTE_SYNTAX"


@pytest.mark.parametrize("bounds", [{"min": "0"}, {"max": 1.5}, {"min": True}])
def test_f3_non_integer_bounds_are_a_type_error(bounds):
    values, _missing, errors = evaluate_all([{"key": "a", "expr": "1", **bounds}], {})
    assert values == {} and errors[0]["code"] == "COMPUTE_TYPE"


def test_f3_valid_entries_after_a_bad_one_still_evaluate():
    values, _missing, errors = evaluate_all(["x", {"key": "b", "expr": "2 * 3", "min": 0}], {})
    assert values == {"computed.b": 6} and len(errors) == 1
