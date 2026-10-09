"""normalize_definition parity with frontend-erp (doc 42 §13/§15).

frontend-erp/lib/playbookPhases.ts ports normalize_definition and runs the
same golden cases (lib/__fixtures__/playbook_normalize.json). The file is
shared byte-for-byte and hash-locked on both sides, so a Python normalizer
change fails here until the fixture, the TS port and BOTH pins move together.
"""
import copy
import hashlib
import json
import pathlib

import pytest

from database_utils.schemas.playbook import normalize_definition

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "playbook_normalize.json"
FIXTURE_SHA256 = "175f834fece54a3deaf524865ccc4e30f90b03a70a3cb88434c088b44824fb00"
CASES = json.loads(FIXTURE.read_text())["cases"]


def test_fixture_is_hash_locked():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256, (
        "playbook_normalize.json changed: update frontend-erp's copy, its TS port "
        "and BOTH hash pins in the same change"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    data = copy.deepcopy(case["input"])
    if case.get("error"):
        with pytest.raises(ValueError, match=case["error"]):
            normalize_definition(data)
        return
    out = normalize_definition(data)
    assert out == case["output"]
    assert list(out) == list(case["output"])  # key order: the YAML view pins it
    assert data == case["input"]  # never mutates its argument
