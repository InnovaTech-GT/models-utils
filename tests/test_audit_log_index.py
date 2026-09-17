"""Registro de actividad (al1_audit_log_created_idx) guardrails.

The revision and AuditLog.__table_args__ declare the same index in two places
that cannot import each other; `alembic check` compares them at migration time
and this compares them at test time. DESC is pinned separately because an
ASC index would still satisfy "the index exists" while losing the whole point
(the timeline is newest-first).

The second half is a NON-regression pin: PR 7 explicitly ships no new audit
columns. `audit_log.company_id` in particular would mean backfilling every
historic row and touching ~120 call sites across two services (07 §2.3).
"""
import importlib.util
import os

from database_utils.models.auth import AuditLog

_HERE = os.path.dirname(__file__)
_VERSIONS = os.path.join(_HERE, "..", "alembic", "versions")


def _al1():
    path = os.path.join(_VERSIONS, "al1_audit_log_created_idx.py")
    spec = importlib.util.spec_from_file_location("al1_audit_log_created_idx", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_position():
    al1 = _al1()
    assert al1.revision == "al1_audit_log_created_idx"
    assert al1.down_revision == "pm1_payment_evidence"


def test_index_declared_in_both_the_revision_and_the_model():
    assert _al1()._NEW_INDEXES == ("ix_audit_log_created_at",)
    index = next(
        i for i in AuditLog.__table__.indexes if i.name == "ix_audit_log_created_at"
    )
    assert [c.name for c in index.columns] == ["created_at"]
    assert index.unique is False


def test_index_is_descending_in_both_places():
    path = os.path.join(_VERSIONS, "al1_audit_log_created_idx.py")
    with open(path) as handle:
        upgrade = handle.read().split("def upgrade()", 1)[1].split("def downgrade()")[0]
    assert "created_at DESC" in upgrade
    index = next(
        i for i in AuditLog.__table__.indexes if i.name == "ix_audit_log_created_at"
    )
    assert "DESC" in str(list(index.expressions)[0]).upper()


def test_revision_is_reversible():
    """Unlike tj1/pm1 this one is a plain index — downgrade must really drop
    it, not print a no-op."""
    path = os.path.join(_VERSIONS, "al1_audit_log_created_idx.py")
    with open(path) as handle:
        downgrade = handle.read().split("def downgrade()", 1)[1]
    assert "DROP INDEX IF EXISTS" in downgrade


def test_no_new_audit_columns():
    columns = set(AuditLog.__table__.columns.keys())
    assert columns == {
        "id", "created_at", "user_id", "action",
        "resource_type", "resource_id", "details", "ip_address",
    }
    # Tenant scoping stays the user_id IN (users of company) join.
    assert "company_id" not in columns
