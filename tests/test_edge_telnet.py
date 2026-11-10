"""Pure EDGE telnet helpers (doc 67 §4.1-4.2, §5)."""
from database_utils.utils.edge_telnet import (
    edge_telnet_ready, edge_telnet_step_problem, extract_identity, host_in_cidr,
    identity_matches, oui_of,
)


def test_host_in_cidr():
    assert host_in_cidr("10.90.3.7", "10.90.0.0/16")
    assert not host_in_cidr("10.70.1.1", "10.90.0.0/16")        # PPPoE side
    for bad in ("onu.local", "fe80::1", "", None, "10.90.3.7:7547"):
        assert not host_in_cidr(bad, "10.90.0.0/16")
    assert not host_in_cidr("10.90.3.7", None)


def test_oui_of():
    assert oui_of("F83451-F660-GPON51356BA9") == "F83451"
    assert oui_of("weird") is None and oui_of(None) is None


def test_identity_matches_diego_onu():
    assert identity_matches("F83451356BA9", "GPON51356BA9", "F83451")
    assert identity_matches("f8:34:51:35:6b:a9", "gpon51356ba9", "f83451")
    assert not identity_matches("AABBCC356BA9", "GPON51356BA9", "F83451")    # OUI differs
    assert not identity_matches("F83451000000", "GPON51356BA9", "F83451")    # tail differs
    assert identity_matches("AABBCC356BA9", "GPON51356BA9", None)            # tail-only fallback
    assert not identity_matches(None, "GPON51356BA9", "F83451")
    assert not identity_matches("F83451356BA9", "GPON5", "F83451")           # serial < 6


def test_extract_identity_show_status():
    rx = r"MAC Address\s*:\s*([0-9A-Fa-f]{12})"
    out = "LAN Configuration\n  MAC Address : F83451356BA9\n"
    assert extract_identity(out, rx) == "F83451356BA9"
    assert extract_identity("nothing", rx) is None


def test_edge_telnet_ready_needs_everything():
    ok = dict(cli_platform="generic_telnet", identity_command="show status",
              identity_regex=r"MAC Address\s*:\s*([0-9A-Fa-f]{12})",
              has_telnet_credential=True, edge_mgmt_cidr="10.90.0.0/16", dial_target="device")
    assert edge_telnet_ready(**ok)
    for key, bad in (("cli_platform", None), ("identity_command", ""), ("identity_regex", None),
                     ("has_telnet_credential", False), ("edge_mgmt_cidr", None),
                     ("dial_target", "gateway")):
        assert not edge_telnet_ready(**{**ok, key: bad})


def test_edge_telnet_step_problem():
    warn = {"name": "catv", "driver": "telnet", "template": "a", "on_failure": "warn"}
    assert edge_telnet_step_problem({}, warn) is None
    assert edge_telnet_step_problem({}, {**warn, "on_failure": None}) == "EDGE_TELNET_REQUIRES_WARN"
    assert edge_telnet_step_problem({"session": {"enable": {"command": "en"}}}, warn) == "EDGE_TELNET_NO_ENABLE"
    assert edge_telnet_step_problem({}, {**warn, "config_mode": True}) == "EDGE_TELNET_NO_ENABLE"
