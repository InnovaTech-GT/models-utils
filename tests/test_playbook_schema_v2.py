"""Playbook format v2 (doc 42 §4, §10.1, §14.1): phases, session, captures,
secrets, outputs, the legacy normalizer and the save-time rules."""
import copy

import pytest
from pydantic import ValidationError

from database_utils.schemas.playbook import (
    SESSION_PROBE_STEP,
    PlaybookDefinition,
    PlaybookOut,
    job_steps,
    mask_log_outputs,
    normalize_definition,
    playbook_warnings,
    sensitive_capture_keys,
    shared_device_wait_errors,
)


def _step(name, **kw):
    return {"name": name, "driver": "simulator", "template": "x"} | kw


def _cli(name, **kw):
    return {"name": name, "driver": "telnet", "template": "show x"} | kw


def _v2(**kw):
    return {"configuration": [_step("cfg")]} | kw


def _err(definition, code):
    with pytest.raises(ValidationError) as exc:
        PlaybookDefinition.model_validate(definition)
    assert code in str(exc.value), str(exc.value)


# ----------------------------------------------------------------- normalizer

def test_v2_input_is_returned_unchanged():
    d = _v2(rollback=[_step("rb")])
    assert normalize_definition(copy.deepcopy(d)) == d


def test_normalizer_is_idempotent():
    legacy = {"steps": [_step("a", on_failure=[_step("undo-a")])],
              "rollback": []}
    once = normalize_definition(legacy)
    assert normalize_definition(once) == once


def test_legacy_steps_become_configuration():
    out = normalize_definition({"variables": [], "steps": [_step("a"), _step("b")]})
    assert "steps" not in out
    assert [s["name"] for s in out["configuration"]] == ["a", "b"]
    assert out["preconditions"] == [] and out["verification"] == []
    assert out["outputs"] == [] and out["secrets"] == []
    assert out["variables"] == []


def test_on_failure_becomes_undoes_gated_rollback_in_reverse():
    out = normalize_definition({"steps": [
        _step("a", on_failure=[_step("undo-a")]),
        _step("b", on_failure=[_step("undo-b1"), _step("undo-b2")]),
        _step("c"),
    ], "rollback": [_step("legacy")]})
    assert all("on_failure" not in s for s in out["configuration"])
    assert [(s["name"], s["undoes"]) for s in out["rollback"]] == [
        ("undo-b1", "b"), ("undo-b2", "b"), ("undo-a", "a")]


def test_legacy_rollback_is_kept_without_undoes():
    out = normalize_definition({"steps": [_step("a")], "rollback": [_step("r")]})
    assert out["rollback"] == [_step("r")]


def test_rollback_names_are_made_unique_across_phases():
    out = normalize_definition({"steps": [_step("s", on_failure=[_step("s")])]})
    names = [s["name"] for s in out["configuration"] + out["rollback"]]
    assert len(set(names)) == 2
    PlaybookDefinition.model_validate(out)


def test_the_connectivity_definition_shape_normalizes():
    conn = {"variables": [], "steps": [
        {"name": "probe", "driver": "ping", "template": "{{device.mgmt_host}}",
         "timeout_seconds": 10}]}
    model = PlaybookDefinition.model_validate(conn)
    assert [s.name for s in model.configuration] == ["probe"]


def test_steps_mirror_round_trip_passes():
    model = PlaybookDefinition.model_validate(_v2())
    mirror = model.model_dump() | {"steps": model.model_dump()["configuration"]}
    again = PlaybookDefinition.model_validate(mirror)
    assert again.configuration[0].name == "cfg"


def test_edited_steps_next_to_configuration_are_refused():
    _err(_v2(steps=[_step("edited")]), "LEGACY_STEPS_CONFLICT")


def test_empty_steps_next_to_configuration_is_v2():
    PlaybookDefinition.model_validate(_v2(steps=[]))


# ----------------------------------------------------------------- shape

def test_stored_dump_is_v2_and_has_no_steps():
    dumped = PlaybookDefinition.model_validate({"steps": [_step("a")]}).model_dump()
    assert "steps" not in dumped
    assert set(dumped) >= {"variables", "computed", "session", "secrets", "preconditions",
                           "configuration", "verification", "rollback", "outputs"}


def test_out_schema_carries_a_read_only_steps_mirror():
    import uuid

    from database_utils.utils.timezone_utils import now_gt
    out = PlaybookOut.model_validate({
        "id": uuid.uuid4(), "company_id": uuid.uuid4(), "version": 1,
        "created_at": now_gt(), "name": "p",
        "definition": _v2(), "is_active": True,
    })
    dumped = out.model_dump()["definition"]
    assert dumped["steps"] == dumped["configuration"]


def test_configuration_is_required():
    _err({"configuration": []}, "at least one")
    _err({"preconditions": [_step("p")]}, "at least one")


def test_names_are_unique_across_all_phases():
    _err(_v2(verification=[_step("cfg")]), "unique")


def test_the_probe_name_is_reserved():
    _err(_v2(preconditions=[_step(SESSION_PROBE_STEP)]), "reserved")


def test_idempotent_round_trips():
    d = {"configuration": [_step("a", idempotent=True)]}
    dumped = PlaybookDefinition.model_validate(d).model_dump()
    assert dumped["configuration"][0]["idempotent"] is True


def test_label_and_hint_are_static_and_bounded():
    PlaybookDefinition.model_validate(
        {"configuration": [_step("a", label="Autorizar ONU", hint="Revise la fibra")]})
    _err({"configuration": [_step("a", label="ONU {{cpe.serial}}")]}, "token")
    _err({"configuration": [_step("a", label="x" * 81)]}, "80")
    _err({"configuration": [_step("a", hint="x" * 201)]}, "200")


# ----------------------------------------------------------------- placement

def test_undoes_only_in_rollback():
    _err(_v2(verification=[_step("v", undoes="cfg")]), "PHASE_FIELD_NOT_ALLOWED")
    PlaybookDefinition.model_validate(_v2(rollback=[_step("r", undoes="cfg")]))


def test_undoes_names_a_configuration_step():
    _err(_v2(rollback=[_step("r", undoes="nope")]), "UNDOES_UNKNOWN_STEP")


def test_capture_not_in_rollback():
    cap = [{"key": "x", "regex": "(a)"}]
    _err(_v2(rollback=[_step("r", capture=cap)]), "PHASE_FIELD_NOT_ALLOWED")


def test_wait_until_placement():
    w = {"tries": 3, "interval_seconds": 10}
    PlaybookDefinition.model_validate(_v2(verification=[_step("v", wait_until=w)]))
    PlaybookDefinition.model_validate(_v2(preconditions=[_step("p", wait_until=w)]))
    _err({"configuration": [_step("c", wait_until=w)]}, "PHASE_FIELD_NOT_ALLOWED")
    _err(_v2(rollback=[_step("r", wait_until=w)]), "PHASE_FIELD_NOT_ALLOWED")
    tr069 = {"name": "set-wifi", "driver": "tr069", "request": {"op": "x"}, "wait_until": w}
    PlaybookDefinition.model_validate({"configuration": [tr069]})


def test_wait_until_bounds():
    _err(_v2(verification=[_step("v", wait_until={"tries": 1, "interval_seconds": 5})]), "")
    _err(_v2(verification=[_step("v", wait_until={"tries": 30, "interval_seconds": 60})]),
         "600")


def test_shared_device_wait_ceiling():
    ok = PlaybookDefinition.model_validate(
        _v2(verification=[_step("v", wait_until={"tries": 12, "interval_seconds": 10})]))
    assert shared_device_wait_errors(ok.model_dump()) == []
    long = PlaybookDefinition.model_validate(
        _v2(verification=[_step("v", wait_until={"tries": 18, "interval_seconds": 10})]))
    errors = shared_device_wait_errors(long.model_dump())
    assert [e["code"] for e in errors] == ["WAIT_TOO_LONG_FOR_SHARED_DEVICE"]


def test_config_mode_needs_a_config_command():
    d = {"configuration": [_cli("c", config_mode=True)]}
    _err(d, "CONFIG_COMMAND_REQUIRED")
    PlaybookDefinition.model_validate(d | {"session": {"config_command": "configure"}})
    _err({"configuration": [_step("c", config_mode=True)],
          "session": {"config_command": "configure"}}, "PHASE_FIELD_NOT_ALLOWED")
    _err(_v2(verification=[_cli("v", config_mode=True)], session={"config_command": "c"}),
         "PHASE_FIELD_NOT_ALLOWED")


def test_session_defaults():
    model = PlaybookDefinition.model_validate(_v2(session={"enable": {}}))
    assert model.session.enable.command == "enable"
    assert model.session.enable.password_prompt == "ssword"
    assert model.session.enable.enabled_prompt == "#"
    assert model.session.exit_command == "exit"
    assert model.session.error_patterns is None
    assert model.session.busy_patterns == []


# ----------------------------------------------------------------- validation + capture

@pytest.mark.parametrize("phase", ["preconditions", "verification", "rollback"])
def test_guard_and_idempotent_are_configuration_only(phase):
    guard = {"template": "show x", "when_met": "skip", "validation": {"expect_contains": "x"}}
    _err(_v2(**{phase: [_step("s", precondition=guard)]}), "PHASE_FIELD_NOT_ALLOWED")
    _err(_v2(**{phase: [_step("s", idempotent=True)]}), "PHASE_FIELD_NOT_ALLOWED")


@pytest.mark.parametrize("good", ["\\(?=x", "\\\\1", "a\\(?<n"])
def test_escaped_text_is_not_refused_as_a_construct(good):
    PlaybookDefinition.model_validate(_v2(preconditions=[_step("p", validation={"expect_regex": good})]))


def test_rendered_validation_and_new_regex_checks():
    v = {"expect_regex": "ONU\\s*:\\s*{{device.out_slot}}", "expect_not_regex": "(?i)authed"}
    PlaybookDefinition.model_validate(_v2(preconditions=[_step("p", validation=v)]))


@pytest.mark.parametrize("bad", [
    "(?=a)b", "(?!a)b", "(?<=a)b", "(?<!a)b", "(a)\\1", "(?P<n>a)", "(?P=n)",
    "(", "a" * 257,
])
def test_regex_refusal_list(bad):
    _err(_v2(preconditions=[_step("p", validation={"expect_regex": bad})]),
         "REGEX_UNSUPPORTED")


@pytest.mark.parametrize("good", ["(?i)online", "(?m)^\\s*RECV", "(?ims)x", "(?:a|b)c"])
def test_leading_inline_flags_are_accepted(good):
    PlaybookDefinition.model_validate(_v2(preconditions=[_step("p", validation={"expect_regex": good})]))


def test_csr_regexes_are_accepted():
    caps = [
        {"key": "onu_serial", "regex": "(?i)\\b({{cpe.serial | alnum}})\\b", "type": "text"},
    ]
    ver = [
        {"key": "onu_state", "regex": "(?i)\\b(online|offline|los|dying_?gasp|auth_?fail\\w*)\\b",
         "type": "text", "equals": "online"},
        {"key": "rx_power", "regex": "(?m)^\\s*RECV POWER\\s*:\\s*(-?\\d+(?:\\.\\d+)?)",
         "type": "number", "min": -27, "max": -8, "unit": "dBm", "label": "Potencia RX"},
    ]
    PlaybookDefinition.model_validate({
        "preconditions": [_cli("onu-visible", capture=caps,
                               wait_until={"tries": 6, "interval_seconds": 10})],
        "configuration": [_cli("Authorize ONU", template="set {{capture.onu_serial}}")],
        "verification": [_cli("online", capture=ver[:1]), _cli("power", capture=ver[1:])],
    })


def test_capture_needs_exactly_one_group():
    _err(_v2(verification=[_step("v", capture=[{"key": "x", "regex": "abc"}])]), "group")
    _err(_v2(verification=[_step("v", capture=[{"key": "x", "regex": "(a)(b)"}])]), "group")


def test_capture_key_rules():
    _err(_v2(verification=[_step("v", capture=[{"key": "wifi_key", "regex": "(a)"}])]),
         "CAPTURE_SECRET_NAME")
    _err(_v2(verification=[_step("v", capture=[{"key": "Bad", "regex": "(a)"}])]), "key")
    two = [{"key": "x", "regex": "(a)"}]
    _err(_v2(preconditions=[_step("p", capture=two)], verification=[_step("v", capture=two)]),
         "declared twice")


def test_capture_limits():
    caps = [{"key": f"c{i}", "regex": "(a)"} for i in range(9)]
    _err(_v2(verification=[_step("v", capture=caps)]), "8")
    many = [_step(f"v{j}", capture=[{"key": f"c{j}_{i}", "regex": "(a)"} for i in range(8)])
            for j in range(5)]
    _err(_v2(verification=many), "32")


def test_thresholds():
    num = {"key": "p", "regex": "(-?\\d+)", "type": "number", "min": "{{computed.min_rx}}",
           "max": -8}
    PlaybookDefinition.model_validate(_v2(
        computed=[{"key": "min_rx", "expr": "0 - 27"}], verification=[_step("v", capture=[num])]))
    _err(_v2(verification=[_step("v", capture=[num | {"min": "{{a}} {{b}}"}])]), "token")
    _err(_v2(verification=[_step("v", capture=[num | {"min": "abc"}])]), "number")
    _err(_v2(verification=[_step("v", capture=[num | {"equals": "x"}])]), "text")
    _err(_v2(verification=[_step("v", capture=[{"key": "s", "regex": "(a)", "type": "text",
                                                "min": 1}])]), "number")
    _err(_v2(verification=[_step("v", capture=[num | {"min": 0, "max": -8}])]), "min")


def test_capture_before_use():
    cap = [{"key": "x", "regex": "(a)"}]
    PlaybookDefinition.model_validate({
        "preconditions": [_step("p", capture=cap)],
        "configuration": [_step("c", template="use {{capture.x}}")],
    })
    _err({"configuration": [_step("c", template="use {{capture.x}}")],
          "verification": [_step("v", capture=cap)]}, "CAPTURE_UNDECLARED")
    _err({"configuration": [_step("c", template="use {{capture.x}}", capture=cap)]},
         "CAPTURE_UNDECLARED")


def test_rollback_reads_a_precondition_capture():
    PlaybookDefinition.model_validate({
        "preconditions": [_step("p", capture=[{"key": "before", "regex": "(a)"}])],
        "configuration": [_step("c")],
        "rollback": [_step("r", template="restore {{capture.before}}", undoes="c")],
    })


# ----------------------------------------------------------------- secrets + outputs

def _wifi(**out):
    return {
        "secrets": [{"key": "wifi_key", "length": 12}],
        "configuration": [{"name": "set-wifi", "driver": "tr069",
                           "request": {"op": "setParameterValues",
                                       "parameters": {"p": ["{{secret.wifi_key}}", "xsd:string"]}}}],
        "outputs": [{"key": "wifi_key", "label": "Clave WiFi", "value": "{{secret.wifi_key}}",
                     "audience": ["technician"], "shareable": True} | out],
    }


def test_a_secret_output_is_always_sensitive():
    model = PlaybookDefinition.model_validate(_wifi())
    out = model.outputs[0]
    assert out.sensitive is True and out.shareable is True
    _err(_wifi(sensitive=False), "sensitive")


def test_secret_must_be_declared():
    d = _wifi()
    d["secrets"] = []
    _err(d, "SECRET_UNDECLARED")


def test_secret_spec_bounds():
    d = _wifi()
    d["secrets"] = [{"key": "wifi_key", "length": 7}]
    _err(d, "length")
    d["secrets"] = [{"key": "wifi_key"}, {"key": "wifi_key"}]
    _err(d, "declared twice")
    assert PlaybookDefinition.model_validate(
        _wifi() | {"secrets": [{"key": "wifi_key"}]}).secrets[0].length == 12


def test_secret_mixed_into_text_is_refused():
    _err(_wifi(value="key={{secret.wifi_key}}"), "OUTPUT_SECRET_MIXED")
    _err(_wifi(value="{{secret.wifi_key | upper}}"), "OUTPUT_SECRET_MIXED")


def test_shareable_needs_the_technician_audience():
    _err(_wifi(audience=["office"]), "OUTPUT_SHARE_AUDIENCE")


def test_output_rules():
    cap = [{"key": "rx_power", "regex": "(-?\\d+)", "type": "number"}]
    base = _v2(verification=[_step("v", capture=cap)])
    out = {"key": "rx_power", "label": "Potencia", "value": "{{capture.rx_power}}",
           "unit": "dBm", "audience": ["technician", "office"]}
    model = PlaybookDefinition.model_validate(base | {"outputs": [out]})
    assert model.outputs[0].sensitive is False and model.outputs[0].shareable is False
    _err(base | {"outputs": [out | {"audience": []}]}, "audience")
    _err(base | {"outputs": [out | {"audience": ["boss"]}]}, "audience")
    _err(base | {"outputs": [out, out]}, "declared twice")
    _err(base | {"outputs": [out | {"value": "{{capture.nope}}"}]}, "CAPTURE_UNDECLARED")
    many = [out | {"key": f"o{i}"} for i in range(17)]
    _err(base | {"outputs": many}, "16")


def test_computed_is_scanned_in_every_phase_and_in_outputs():
    _err(_v2(rollback=[_step("r", template="{{computed.nope}}")]), "COMPUTE_NAME")
    _err(_v2(verification=[_step("v", validation={"expect_contains": "{{computed.nope}}"})]),
         "COMPUTE_NAME")
    _err(_v2(outputs=[{"key": "o", "label": "o", "value": "{{computed.nope}}",
                       "audience": ["office"]}]), "COMPUTE_NAME")
    cap = [{"key": "p", "regex": "(\\d+)", "type": "number", "min": "{{computed.nope}}"}]
    _err(_v2(verification=[_step("v", capture=cap)]), "COMPUTE_NAME")


# ----------------------------------------------------------------- job_steps / warnings

def test_job_steps_per_phase_and_flattened():
    d = PlaybookDefinition.model_validate(_v2(
        preconditions=[_cli("p")], verification=[_cli("v")], rollback=[_step("r")])).model_dump()
    assert [s["name"] for s in job_steps(d, "CONFIGURATION")] == ["cfg"]
    assert [s["name"] for s in job_steps(d, "ROLLBACK")] == ["r"]
    assert [s["name"] for s in job_steps(d, None)] == ["p", "cfg", "v"]
    probe = job_steps(d, "PRECONDITIONS", probe=True)
    assert [s["name"] for s in probe] == [SESSION_PROBE_STEP, "p"]
    assert probe[0]["driver"] == "telnet" and probe[0]["template"] == ""
    # Legacy input is normalized first.
    assert [s["name"] for s in job_steps({"steps": [_step("a")]}, "CONFIGURATION")] == ["a"]


def _codes(warnings):
    return [w["code"] for w in warnings]


def test_warnings():
    d = {"configuration": [_cli("c")]}
    assert "ROLLBACK_EMPTY" in _codes(playbook_warnings(d))
    assert "ROLLBACK_EMPTY" not in _codes(playbook_warnings({"configuration": [_step("c")]}))
    assert "ROLLBACK_WITHOUT_UNDOES" in _codes(playbook_warnings(d | {"rollback": [_cli("r")]}))
    assert "ENABLE_WITHOUT_SESSION" in _codes(
        playbook_warnings({"configuration": [_cli("c", template="enable")]}))
    assert "NOT_RESEND_SAFE" in _codes(playbook_warnings(d))
    assert "NOT_RESEND_SAFE" not in _codes(
        playbook_warnings({"configuration": [_cli("c", idempotent=True)]}))
    cpe = {"preconditions": [_cli("p")], "configuration": [_step("c")]}
    assert "CPE_NETWORK_PRECONDITION" in _codes(
        playbook_warnings(cpe, category_tier="EDGE", purpose="ACTIVATION"))
    assert "CPE_NETWORK_PRECONDITION" not in _codes(
        playbook_warnings(cpe, category_tier="EDGE", purpose="DEPROVISION"))
    assert "CPE_NETWORK_PRECONDITION" not in _codes(
        playbook_warnings(cpe, category_tier="CORE", purpose="ACTIVATION"))


def test_job_out_masks_sensitive_log_outputs():
    """doc 42 §10.1: a sensitive value never leaves the API, even from a
    child's job.log.outputs."""
    import uuid
    from datetime import datetime

    from database_utils.schemas.playbook import ProvisioningJobOut

    log = {"steps": [], "outputs": [
        {"key": "ip", "value": "10.1.4.84", "sensitive": True},
        {"key": "wifi_key", "value": None, "secret": True, "ref": "secret.wifi_key"},
        {"key": "power", "value": "-21", "sensitive": False},
    ]}
    out = ProvisioningJobOut(
        id=uuid.uuid4(), company_id=uuid.uuid4(), playbook_id=uuid.uuid4(),
        status="SUCCEEDED", attempts=1, max_attempts=3, triggered_by="USER",
        created_at=datetime.now(), log=log)
    assert [o["value"] for o in out.log["outputs"]] == [None, None, "-21"]
    assert "10.1.4.84" not in out.model_dump_json()
    assert log["outputs"][0]["value"] == "10.1.4.84"  # the stored log is untouched


# ----------------------------------------------------------------- sensitive captures

def test_sensitive_capture_keys_are_the_captures_a_non_secret_sensitive_output_reads():
    d = {"outputs": [
        {"key": "ip", "value": "{{ capture.ip }}/{{capture.mask}}", "sensitive": True},
        {"key": "rx", "value": "{{capture.rx}}"},
        {"key": "wifi", "value": "{{secret.wifi_key}}", "sensitive": True},
    ]}
    assert sensitive_capture_keys(d) == ["ip", "mask"]
    assert sensitive_capture_keys({}) == []


def test_mask_log_outputs_blanks_sensitive_captures_everywhere():
    log = {"sensitive_captures": ["ip"], "captures": {"ip": "10.0.0.9", "rx": "-20"},
           "steps": [{"name": "a", "captures": {"ip": "10.0.0.9"}}, {"name": "b"}],
           "rollback_steps": [{"name": "r", "captures": {"rx": "-20"}}],
           "outputs": [{"key": "ip", "value": "10.0.0.9", "sensitive": True}]}
    out = mask_log_outputs(log)
    assert out["captures"] == {"ip": None, "rx": "-20"}
    assert out["steps"] == [{"name": "a", "captures": {"ip": None}}, {"name": "b"}]
    assert out["rollback_steps"][0]["captures"] == {"rx": "-20"}
    assert out["outputs"][0]["value"] is None
    assert log["captures"]["ip"] == "10.0.0.9", "the stored log is not mutated"
