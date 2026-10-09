"""pe1_playbook_phases (doc 42 §12) guardrails: the CHECK literals shared by
database_utils/models/isp.py and the hand-written migration are duplicated on
purpose (revisions are immutable, models are not) and pinned byte-identical
here, so a drift edit fails CI instead of `alembic upgrade head` on prod."""
import importlib.util
import os

from database_utils.models import isp
from database_utils.models.isp import DeviceCredential, ProvisioningJob, ProvisioningRun

_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions",
                     "pe1_playbook_phases.py")


def _load_pe1():
    spec = importlib.util.spec_from_file_location("pe1_playbook_phases", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_enable_is_a_credential_kind():
    assert "CLI_ENABLE" in isp.CREDENTIAL_KINDS
    for kind in isp.CREDENTIAL_KINDS:
        assert f"'{kind}'" in isp._CREDENTIAL_KIND_CHECK


def test_credential_check_literal_matches_the_migration():
    pe1 = _load_pe1()
    assert pe1._CREDENTIAL_KIND_CHECK == isp._CREDENTIAL_KIND_CHECK
    assert "'CLI_ENABLE'" not in pe1._CREDENTIAL_KIND_CHECK_PRE


def test_phase_check_literal_matches_the_migration():
    pe1 = _load_pe1()
    assert pe1._PROVISIONING_PHASE_CHECK == isp._PROVISIONING_PHASE_CHECK
    for phase in isp.PROVISIONING_PHASES:
        assert f"'{phase}'" in isp._PROVISIONING_PHASE_CHECK


def test_phase_constants():
    assert isp.PROVISIONING_PHASES == (
        "PRECONDITIONS", "CONFIGURATION", "VERIFICATION", "ROLLBACK")
    assert isp.TEARDOWN_PURPOSES == {"SUSPENSION", "DEPROVISION"}


def test_new_columns_are_nullable():
    run = ProvisioningRun.__table__.c
    for col in ("phase", "error_code", "error", "outputs", "secrets_ciphertext",
                "secrets_dek_wrapped", "secrets_kek_id"):
        assert run[col].nullable is True, col
    assert ProvisioningJob.__table__.c["phase"].nullable is True


def test_check_constraints_are_named():
    names = {c.name for c in ProvisioningRun.__table__.constraints}
    assert "ck_provisioning_run_phase" in names
    names = {c.name for c in ProvisioningJob.__table__.constraints}
    assert "ck_provisioning_job_phase" in names
    names = {c.name for c in DeviceCredential.__table__.constraints}
    assert "ck_device_credential_kind" in names


def test_migration_chain_position():
    pe1 = _load_pe1()
    assert pe1.revision == "pe1_playbook_phases"
    # Program chain (doc 42a §4): tl1 -> oa1 -> pe1 (re-pointed at compose, 6.6.0).
    assert pe1.down_revision == "oa1_task_onu_auto_assigned"
