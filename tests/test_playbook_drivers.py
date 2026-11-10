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
    ("EDGE", {"telnet", "tr069", "connection_request", "ping", "http", "simulator", "manual"}),
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
                 "allowed": ["http", "ping", "simulator", "snmp", "ssh", "telnet"], "reason": None}]
    assert mismatches(d, "CORE") == expected
    assert mismatches(PlaybookDefinition.model_validate(d), "CORE") == expected


def test_mismatches_walks_every_phase():
    d = {"configuration": [{"name": "a", "driver": "ssh", "template": "x"}],
         "verification": [{"name": "v", "driver": "tr069", "template": "x"}],
         "rollback": [{"name": "r", "driver": "tr069", "template": "x"}]}
    found = [(m["phase"], m["step_index"]) for m in mismatches(d, "CORE")]
    assert found == [("verification", 0), ("rollback", 0)]


def test_connection_request_step_needs_request_and_renders_it():
    from database_utils.schemas.playbook import PlaybookStep
    from database_utils.utils.playbook_render import _render_step
    with pytest.raises(ValueError, match="connection_request driver requires 'request'"):
        PlaybookStep(name="s", driver="connection_request", template="x")
    step = PlaybookStep(name="s", driver="connection_request",
                        request={"serial": "{{serial}}", "username": "u", "password": "p"})
    rendered, problems = _render_step(step.model_dump(), {"serial": "ABC"},
                                      "connection_request")
    assert rendered["serial"] == "ABC" and not problems


# --- EDGE telnet (doc 67): gated by edge_telnet_ok, best effort, enable-free ---

def test_edge_telnet_offered_only_when_ready():
    opts = {o["key"]: o for o in driver_options("EDGE", "device", "none")}
    assert opts["telnet"] == {"key": "telnet", "allowed": False,
                              "reason": "EDGE_TELNET_NOT_CONFIGURED", "probe": None}
    opts = {o["key"]: o for o in driver_options("EDGE", "device", "none", edge_telnet_ok=True)}
    assert opts["telnet"]["allowed"] and opts["telnet"]["reason"] is None
    assert not opts["ssh"]["allowed"]


def test_edge_telnet_mismatch_unless_ready():
    d = {"configuration": [{"name": "catv", "driver": "telnet", "template": "config catv disable",
                            "on_failure": "warn"}]}
    [m] = mismatches(d, "EDGE")
    assert m["driver"] == "telnet" and m["reason"] == "EDGE_TELNET_NOT_CONFIGURED"
    assert "telnet" not in m["allowed"]
    assert mismatches(d, "EDGE", edge_telnet_ok=True) == []
    assert mismatches(d, "CORE") == []


def test_edge_telnet_hard_step_is_mismatch_even_when_ready():
    d = {"configuration": [{"name": "catv", "driver": "telnet", "template": "config catv disable"}]}
    [m] = mismatches(d, "EDGE", edge_telnet_ok=True)
    assert m["reason"] == "EDGE_TELNET_REQUIRES_WARN" and "telnet" in m["allowed"]
    assert mismatches(d, "CORE") == []


def test_edge_telnet_enable_or_config_mode_is_mismatch():
    warn = {"name": "catv", "driver": "telnet", "template": "a", "on_failure": "warn"}
    for d in ({"session": {"enable": {"command": "enable"}}, "configuration": [warn]},
              {"session": {"config_command": "conf t"}, "configuration": [warn | {"config_mode": True}]}):
        assert mismatches(d, "EDGE", edge_telnet_ok=True)[0]["reason"] == "EDGE_TELNET_NO_ENABLE"


def test_tier_mismatch_entry_has_null_reason():
    d = {"configuration": [{"name": "x", "driver": "tr069", "template": "a"}]}
    assert mismatches(d, "CORE")[0]["reason"] is None
