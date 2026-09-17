"""Red/provisioning (ng2_provisioning_run_list) guardrails.

The revision and ProvisioningRun.__table_args__ declare the same index in two
places that cannot import each other; this compares them at test time.
"""
import importlib.util
import os

from database_utils.models.isp import ProvisioningRun

_HERE = os.path.dirname(__file__)
_PATH = os.path.join(_HERE, "..", "alembic", "versions", "ng2_provisioning_run_list.py")


def _ng2():
    spec = importlib.util.spec_from_file_location("ng2_provisioning_run_list", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_position():
    ng2 = _ng2()
    assert ng2.revision == "ng2_provisioning_run_list"
    assert ng2.down_revision == "al1_audit_log_created_idx"


def test_index_declared_in_both_the_revision_and_the_model():
    assert _ng2()._NEW_INDEXES == ("ix_provisioning_run_company_created",)
    index = next(
        i for i in ProvisioningRun.__table__.indexes
        if i.name == "ix_provisioning_run_company_created"
    )
    assert [c.name for c in index.columns] == ["company_id", "created_at"]
    assert index.unique is False


def test_revision_is_reversible():
    with open(_PATH) as handle:
        body = handle.read()
    assert "CREATE INDEX IF NOT EXISTS" in body
    assert "DROP INDEX IF EXISTS" in body.split("def downgrade()", 1)[1]
