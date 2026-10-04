"""cr1: revision <-> model parity, chain position, ADMIN-only permission."""
from database_utils.models import CashSession, CashSessionStatus
from database_utils.schemas.mobile_integration import CashSessionOut

from _mi_helpers import isp_seed, load


def _cr1():
    return load("versions/cr1_cash_review.py", "cr1_test")


def test_chain_position():
    assert _cr1().down_revision == "pd1_client_payment_day"
    assert len(_cr1().revision) <= 32


def test_labels_match_enum_and_legacy_kept():
    assert {CashSessionStatus(v) for v in _cr1().NEW_LABELS} == {
        CashSessionStatus.SUBMITTED, CashSessionStatus.REJECTED, CashSessionStatus.APPROVED}
    assert CashSessionStatus.CLOSED and CashSessionStatus.DEPOSITED


def test_columns_match_model():
    cols = CashSession.__table__.c
    for name, _ in _cr1()._COLUMNS:
        assert name in cols
    assert cols.reviewed_by.foreign_keys


def test_permission_declared_once_and_not_granted_to_base_roles():
    rbac = load("seeds/rbac_seed.py", "rbac_cr1_test")
    rows = [p for p in rbac.PERMISSIONS_DATA if p["name"] == "cash_sessions.review"]
    assert rows == [_cr1().PERMISSION]
    for spec in isp_seed().ISP_ROLES.values():
        assert "cash_sessions.review" not in spec["permissions"]


def test_out_schema_has_review_fields():
    assert {"submitted_at", "reviewed_at", "reviewed_by_name", "review_note"} <= set(CashSessionOut.model_fields)
