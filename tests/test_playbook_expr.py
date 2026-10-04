"""playbook_expr + the declared `computed` block (doc 40 §3.3.3).

The fixture is hash-locked: frontend-erp/lib/playbookExpr.ts runs the same
file (copied to lib/__fixtures__/) and pins the same hash, so the two
implementations cannot drift silently.
"""
import hashlib
import json
import pathlib
import re

import pytest
from pydantic import ValidationError

from database_utils.schemas.playbook import PlaybookDefinition
from database_utils.utils import playbook_expr
from database_utils.utils.playbook_expr import (
    evaluate_all,
    is_secret_name,
    names,
    parse,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "playbook_expr.json"
FIXTURE_SHA256 = "d4f843d9fb7efc52938fe52270cd3b46489b1d08814098aec0c96656945f88c0"
CASES = json.loads(FIXTURE.read_text())["cases"]


def test_fixture_is_hash_locked():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256, (
        "playbook_expr.json changed: update the TypeScript mirror, its fixture "
        "copy and BOTH hash pins in the same change"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    values, missing, errors = evaluate_all(case["computed"], case["variables"])
    assert values == case["values"]
    assert missing == case["missing"]
    assert [{"key": e["key"], "code": e["code"]} for e in errors] == case["errors"]


def test_fixture_covers_the_required_cases():
    names_ = " ".join(c["name"] for c in CASES)
    for needle in ("csr", "precedence", "unary minus", "truncates", "division by zero",
                   "overflow", "range", "256 characters", "65 tokens", "paren depth 9",
                   "power", "dunder", "subscript", "quotes", "float", "boolean",
                   "trailing newline", "secret", "input namespace"):
        assert needle in names_, needle


def test_parse_returns_plain_tuples_and_names():
    tree = parse("(path.mufa_principal.out_port - 1) * 16 + path.mufa_secundaria.out_port")
    assert tree[0] == "+"
    assert names(tree) == ["path.mufa_principal.out_port", "path.mufa_secundaria.out_port"]


def test_no_dynamic_evaluation_in_the_source():
    source = pathlib.Path(playbook_expr.__file__).read_text()
    for forbidden in ("eval(", "exec(", "import ast", ".format(", "__import__", "getattr("):
        assert forbidden not in source, forbidden
    # re.compile is fine; the builtin compile() is not.
    assert not re.search(r"(?<!re\.)\bcompile\(", source)


def test_is_secret_name():
    assert is_secret_name("path.olt.password")
    assert is_secret_name("API_TOKEN")
    assert not is_secret_name("path.mufa_principal.out_port")


# --------------------------------------------------- save-time (PlaybookDefinition)

_STEP = {"name": "s", "driver": "simulator", "template": "onu {{computed.onu_id}}"}
_CSR = {"key": "onu_id", "min": 1, "max": 128,
        "expr": "(path.mufa_principal.out_port - 1) * 16 + path.mufa_secundaria.out_port"}


def _definition(computed, steps=None, rollback=None):
    return PlaybookDefinition.model_validate(
        {"computed": computed, "steps": steps or [_STEP], "rollback": rollback or []}
    )


def test_a_valid_computed_block_round_trips_through_model_dump():
    d = _definition([_CSR])
    assert d.model_dump()["computed"] == [_CSR]


def test_computed_defaults_to_empty():
    assert PlaybookDefinition.model_validate({"steps": [{"name": "s", "driver": "simulator",
                                                          "template": "x"}]}).computed == []


@pytest.mark.parametrize("computed, needle", [
    ([{**_CSR, "expr": "2 ** 3"}], "COMPUTE_SYNTAX"),
    ([{**_CSR, "expr": "input.vlan + 1"}], "COMPUTE_NAME"),
    ([{**_CSR, "expr": "path.olt.password + 1"}], "COMPUTE_SECRET"),
    ([{**_CSR, "key": "api_token"}], "secret-named"),
    ([{**_CSR, "key": "Onu"}], "computed key must match"),
    ([{**_CSR, "key": "a" * 33}], "computed key must match"),
    ([{**_CSR, "min": 10, "max": 1}], "min must not exceed max"),
    ([{**_CSR, "min": True}], "min"),
    ([_CSR, _CSR], "declared twice"),
    ([{"key": "a", "expr": "computed.onu_id + 1"}, _CSR], "earlier computed keys"),
    ([{"key": f"k{i}", "expr": "1"} for i in range(16)] + [_CSR], "COMPUTE_LIMIT"),
])
def test_save_time_refusals(computed, needle):
    with pytest.raises(ValidationError, match=needle):
        _definition(computed)


def test_every_computed_token_must_be_declared():
    with pytest.raises(ValidationError, match="not declared"):
        _definition([])
    with pytest.raises(ValidationError, match="not declared"):
        _definition([_CSR], rollback=[{"name": "r", "driver": "simulator",
                                       "template": "{{ computed.svlan | pad_left:3 }}"}])


def test_computed_tokens_in_requests_and_on_failure_are_checked():
    step = {"name": "h", "driver": "http",
            "request": {"method": "POST", "path": "/x", "body": {"id": "{{computed.nope}}"}}}
    with pytest.raises(ValidationError, match="not declared"):
        _definition([_CSR], steps=[_STEP, step])


def test_evaluate_all_accepts_validated_models():
    computed = _definition([_CSR]).computed
    values, missing, errors = evaluate_all(computed, {
        "path.mufa_principal.out_port": 3, "path.mufa_secundaria.out_port": 5})
    assert (values, missing, errors) == ({"computed.onu_id": 37}, [], [])
