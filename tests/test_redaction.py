"""
Secret redaction tests (Cycle 5 Phase 1, canon C14/C19).

Moved from backend-erp tests/provisioning/test_redaction.py (provisioning worker split, R1).
The executor-integration cases (drivers + execute_playbook) and the
utils.secret_safe_audit cases stay in backend-erp.
"""
from database_utils.utils.playbook_render import redact_secrets


def test_redact_secrets_scrubs_literal_values_in_strings():
    text = "username=admin password=hunter2 done"
    redacted = redact_secrets(text, ["hunter2"])
    assert "hunter2" not in redacted
    assert "***" in redacted


def test_redact_secrets_recurses_into_dict_and_list():
    value = {"a": "hunter2", "b": ["ok", "hunter2-suffix"], "c": {"d": "hunter2"}}
    redacted = redact_secrets(value, ["hunter2"])
    assert redacted["a"] == "***"
    assert redacted["b"][1] == "***-suffix"
    assert redacted["c"]["d"] == "***"


def test_redact_secrets_ignores_empty_values():
    text = "nothing to redact"
    assert redact_secrets(text, ["", "   ", None]) == text


# --- security audit #96 (founder D6): wider hints + declared secrets -----------
import pytest  # noqa: E402

from database_utils.schemas.playbook import PlaybookVariable  # noqa: E402
from database_utils.utils.playbook_expr import is_secret_name  # noqa: E402
from database_utils.utils.playbook_template import declared_secret_names, mask_secrets  # noqa: E402


@pytest.mark.parametrize("name", ["input.wifi_pass", "input.wpa_phrase", "input.ppp_pwd", "input.psk",
                                  "input.wifi_passphrase", "input.PASSWORD"])
def test_widened_hints(name):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["cpe.serial", "device.is_passive", "path.olt.mgmt_host", "input.ping_count",
                                  "input.mapping"])
def test_non_secrets_unchanged(name):
    assert not is_secret_name(name)


def test_declared_secret_masked():
    d = {"variables": [{"key": "clave", "secret": True}, {"key": "vlan"}]}
    assert declared_secret_names(d) == frozenset({"input.clave"})
    assert mask_secrets({"input.clave": "x", "input.vlan": 10}, declared_secret_names(d)) == {
        "input.clave": "***", "input.vlan": 10}
    assert mask_secrets({"input.clave": "x"}) == {"input.clave": "x"}


def test_declared_secret_names_tolerates_odd_definitions():
    assert declared_secret_names({}) == frozenset()
    assert declared_secret_names(None) == frozenset()
    assert declared_secret_names({"variables": [{"secret": True}, "junk"]}) == frozenset()


def test_playbook_variable_secret_flag():
    assert PlaybookVariable(key="clave").secret is False
    assert PlaybookVariable(key="clave", secret=True).secret is True
