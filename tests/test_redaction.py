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
