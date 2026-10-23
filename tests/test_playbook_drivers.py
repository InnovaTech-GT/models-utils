"""Rule matrix for context-aware playbook drivers (playbook_drivers.py).

Moved from backend-erp tests/provisioning/test_playbook_drivers.py (provisioning worker split, R1).
The reject_driver_mismatch (422) cases stay in backend-erp.
"""
import pytest

from database_utils.utils.playbook_drivers import (
    ALLOWED_BY_TIER, EDITOR_DRIVERS, allowed_drivers, driver_options,
    mismatches, ping_probe,
)


@pytest.mark.parametrize("tier,expected", [
    ("CORE", {"ssh", "telnet", "snmp", "ping", "http", "simulator"}),
    ("EDGE", {"tr069", "ping", "http", "simulator", "manual"}),
    ("CONSUMABLE", {"ping", "simulator"}),
    ("TOOL", {"ping", "simulator"}),
    ("OTHER", {"ping", "simulator"}),
    (None, {"ping", "simulator"}),
    ("UNKNOWN_TIER", {"ping", "simulator"}),
])
def test_allowed_by_tier(tier, expected):
    assert allowed_drivers(tier) == expected


def test_matrix_covers_all_tiers():
    assert set(ALLOWED_BY_TIER) == {"CORE", "EDGE", "CONSUMABLE", "TOOL", "OTHER", None}


@pytest.mark.parametrize("dial,proxy,expected", [
    ("device", "none", "icmp"),
    ("gateway", "none", "tcp"),
    ("device", "socks5", "tcp"),
    ("gateway", "socks5", "tcp"),
])
def test_ping_probe(dial, proxy, expected):
    assert ping_probe(dial, proxy) == expected


def test_driver_options_shape_and_order():
    opts = driver_options("EDGE", "device", "socks5")
    assert [o["key"] for o in opts] == list(EDITOR_DRIVERS)
    by_key = {o["key"]: o for o in opts}
    assert by_key["tr069"] == {"key": "tr069", "allowed": True, "reason": None, "probe": None}
    assert by_key["ssh"] == {"key": "ssh", "allowed": False, "reason": "TIER_MISMATCH", "probe": None}
    assert by_key["ping"]["probe"] == "tcp"
    assert all(o["probe"] is None for o in opts if o["key"] != "ping")


def test_mismatches_accepts_dict_and_model():
    from database_utils.schemas.playbook import PlaybookDefinition
    d = {"steps": [{"name": "a", "driver": "simulator", "template": "x"},
                   {"name": "b", "driver": "tr069", "template": "x"}]}
    expected = [{"phase": "configuration", "step_index": 1, "driver": "tr069", "tier": "CORE",
                 "allowed": ["http", "ping", "simulator", "snmp", "ssh", "telnet"]}]
    assert mismatches(d, "CORE") == expected
    assert mismatches(PlaybookDefinition.model_validate(d), "CORE") == expected


def test_mismatches_walks_every_phase():
    d = {"configuration": [{"name": "a", "driver": "ssh", "template": "x"}],
         "verification": [{"name": "v", "driver": "tr069", "template": "x"}],
         "rollback": [{"name": "r", "driver": "tr069", "template": "x"}]}
    found = [(m["phase"], m["step_index"]) for m in mismatches(d, "CORE")]
    assert found == [("verification", 0), ("rollback", 0)]
