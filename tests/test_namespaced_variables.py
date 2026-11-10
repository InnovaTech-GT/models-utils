"""
Namespaced playbook variables (doc 33) — the renderer grammar and the
unresolved-token guard.

The grammar is the load-bearing part of the migration: a token the pattern
does NOT match is never reported missing, it is emitted as literal text. That
is how `{{edge_devices[0].serial}}` would reach a device verbatim if the
pattern had not been widened alongside the emission change.

Moved from backend-erp tests/provisioning/test_namespaced_variables.py (provisioning worker split, R1).
"""
import pytest

from database_utils.utils.playbook_render import _leftover_tokens, _render_step, dry_run_playbook
from database_utils.utils.playbook_template import (
    find_variables,
    input_key,
    input_problem,
    render_string,
    render_value,
    resolve_variables,
)


class TestGrammar:
    @pytest.mark.parametrize("token", [
        "edge_devices[0].serial",
        "core_devices[12].mgmt_host",
        "chain[1].item_id",
        "service_plan.vlan",
        "service_plan.download_mbps",
        "client.name",
        "service.id",
        "input.probe_command",
        "plain_name",
    ])
    def test_recognized(self, token):
        assert find_variables("x {{%s}} y" % token) == [token]

    @pytest.mark.parametrize("token", [
        "Edge_Devices[0].serial",   # uppercase
        "0edge.serial",             # leading digit
        "edge_devices[].serial",    # empty index
        "edge_devices[a].serial",   # non-numeric index
    ])
    def test_not_recognized(self, token):
        assert find_variables("{{%s}}" % token) == []

    def test_whitespace_tolerated(self):
        assert find_variables("{{  service_plan.vlan  }}") == ["service_plan.vlan"]

    def test_substitution_is_a_flat_dict_lookup(self):
        """The whole dotted/indexed token is ONE key — nothing is walked or
        evaluated, which is what keeps ADR-006 intact."""
        out, missing = render_string(
            "vlan {{service_plan.vlan}} ont {{edge_devices[0].serial}}",
            {"service_plan.vlan": 110, "edge_devices[0].serial": "ONU-1"},
        )
        assert out == "vlan 110 ont ONU-1"
        assert missing == []

    def test_nested_lookalike_is_not_traversed(self):
        """A nested dict under a namespace prefix must NOT satisfy a dotted
        token — otherwise the renderer would be walking objects."""
        out, missing = render_string(
            "{{service_plan.vlan}}", {"service_plan": {"vlan": 110}}
        )
        assert missing == ["service_plan.vlan"]
        assert out == "{{service_plan.vlan}}"

    def test_missing_namespaced_token_is_reported(self):
        _out, missing = render_string("{{edge_devices[3].serial}}", {})
        assert missing == ["edge_devices[3].serial"]

    def test_render_value_recurses_into_request_payloads(self):
        out, missing = render_value(
            {"body": {"serial": "{{edge_devices[0].serial}}"}, "n": [1, "{{client.name}}"]},
            {"edge_devices[0].serial": "S1", "client.name": "Ada"},
        )
        assert out == {"body": {"serial": "S1"}, "n": [1, "Ada"]}
        assert missing == []


class TestInputNamespace:
    def test_declared_variables_are_namespaced_at_reference(self):
        definition = {"variables": [{"key": "vlan", "default": 10}]}
        resolved, missing = resolve_variables(definition, None)
        assert resolved == {"input.vlan": 10}
        assert missing == []

    def test_caller_may_supply_bare_or_namespaced(self):
        definition = {"variables": [{"key": "vlan", "required": True}]}
        bare, _ = resolve_variables(definition, {"vlan": 20})
        namespaced, _ = resolve_variables(definition, {"input.vlan": 20})
        assert bare["input.vlan"] == namespaced["input.vlan"] == 20

    def test_missing_required_reports_the_bare_key_the_author_typed(self):
        definition = {"variables": [{"key": "vlan", "required": True}]}
        _resolved, missing = resolve_variables(definition, {})
        assert missing == ["vlan"]

    def test_system_variables_pass_through_untouched(self):
        resolved, _ = resolve_variables({}, {"edge_devices[0].serial": "S1"})
        assert resolved["edge_devices[0].serial"] == "S1"

    def test_input_key_is_idempotent(self):
        assert input_key("vlan") == "input.vlan"
        assert input_key("input.vlan") == "input.vlan"


class TestLeftoverTokenGuard:
    """A token the grammar cannot see would otherwise be pushed to a device
    as literal text. The guard turns that into a loud failure."""

    def test_unmatched_token_shape_is_caught(self):
        assert _leftover_tokens("set serial {{Edge_Devices[0].serial}}") == [
            "{{Edge_Devices[0].serial}}"
        ]

    def test_json_braces_are_not_flagged(self):
        assert _leftover_tokens('{"a": {"b": 1}}') == []

    def test_render_step_reports_both_missing_and_leftovers(self):
        rendered, unresolved = _render_step(
            {"template": "a {{service_plan.vlan}} b {{Bad_Token}}"}, {}, "simulator"
        )
        assert "service_plan.vlan" in unresolved
        assert "{{Bad_Token}}" in unresolved
        assert "{{" in rendered  # nothing was substituted

    def test_retired_flat_name_fails_loudly_rather_than_silently(self):
        """A legacy `device1_serial` left behind by a bad migration must not
        render as empty or literal — it must stop the step."""
        _rendered, unresolved = _render_step(
            {"template": "serial {{device1_serial}}"},
            {"edge_devices[0].serial": "S1"},
            "simulator",
        )
        assert unresolved == ["device1_serial"]

    def test_fully_resolved_step_reports_nothing(self):
        rendered, unresolved = _render_step(
            {"template": "serial {{edge_devices[0].serial}}"},
            {"edge_devices[0].serial": "S1"},
            "simulator",
        )
        assert rendered == "serial S1"
        assert unresolved == []

    def test_guard_reaches_into_request_payloads(self):
        _rendered, unresolved = _render_step(
            {"request": {"body": {"s": "{{Bad_Token}}"}}}, {}, "http"
        )
        assert unresolved == ["{{Bad_Token}}"]


# ------------------------------------------------- playbook inputs (6.25.2)

KP = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase"
WIFI_VAR = {"key": "wifi_password", "label": "tu nueva clave WiFi", "type": "TEXT",
            "required": True, "default": None, "min_length": 8, "max_length": 63}
# The dev CGG-F782CW WIFI_PASSWORD definition (v4).
CGG_WIFI = {
    "variables": [WIFI_VAR],
    "configuration": [{"name": "set-wifi-password", "driver": "tr069",
                       "request": {"op": "setParameterValues",
                                   "parameters": {KP: ["{{input.wifi_password}}", "xsd:string"]}}}],
}
STAND_IN = "«input.wifi_password»"


class TestDryRunStandIns:
    def test_a_dry_run_stands_in_a_missing_text_input(self):
        resolved, missing = resolve_variables(CGG_WIFI, {}, dry_run=True)
        assert resolved["input.wifi_password"] == STAND_IN
        assert missing == []

    def test_a_missing_number_input_is_still_missing_in_a_dry_run(self):
        definition = {"variables": [{"key": "vlan", "type": "NUMBER", "required": True}]}
        resolved, missing = resolve_variables(definition, {}, dry_run=True)
        assert missing == ["vlan"] and "input.vlan" not in resolved

    def test_a_live_call_without_the_input_is_unchanged(self):
        _resolved, missing = resolve_variables(CGG_WIFI, {})
        assert missing == ["wifi_password"]

    @pytest.mark.parametrize("key", ["wifi_password", "input.wifi_password"])
    def test_a_stand_in_is_never_a_live_value(self, key):
        resolved, missing = resolve_variables(CGG_WIFI, {key: STAND_IN})
        assert missing == ["wifi_password"]
        assert STAND_IN not in resolved.values()

    def test_dry_run_playbook_renders_the_stand_in(self):
        report = dry_run_playbook(CGG_WIFI, {})
        assert report["valid"] is True
        assert report["missing_variables"] == []
        assert STAND_IN in str(report)


@pytest.mark.parametrize("var,value,expected", [
    (WIFI_VAR, None, "MISSING_INPUT"),
    (WIFI_VAR, "", "MISSING_INPUT"),
    (WIFI_VAR, STAND_IN, "MISSING_INPUT"),
    ({"key": "k", "required": True, "default": "x"}, "", None),
    ({"key": "k", "required": False}, None, None),
    (WIFI_VAR, "abc", "INVALID_INPUT"),
    (dict(WIFI_VAR, max_length=63), "a" * 64, "INVALID_INPUT"),
    (WIFI_VAR, "Prueba2026!", None),
    ({"key": "vlan", "type": "NUMBER", "required": True}, 10, None),
])
def test_input_problem(var, value, expected):
    assert input_problem(var, value) == expected
