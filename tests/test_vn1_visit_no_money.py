"""vn1_visit_no_money (doc 68 §2): the revision chain and the `noMoney`
collection visit code, bound by value through the model and the visit schema."""
from _mi_helpers import load
from database_utils.models.crm import CollectionVisit, CollectionVisitCode
from database_utils.schemas.mobile_integration import CollectionVisitCreate


def test_vn1_revision_chain():
    m = load("versions/vn1_visit_no_money.py", "vn1_visit_no_money")
    assert m.revision == "vn1_visit_no_money" and m.down_revision == "et1_edge_telnet"


def test_no_money_code_value_and_binding():
    assert CollectionVisitCode("noMoney") is CollectionVisitCode.NO_MONEY
    assert "noMoney" in CollectionVisit.__table__.c.visit_code.type.enums
    body = CollectionVisitCreate(outcome="NO_CONTACT", visit_code="noMoney")
    assert body.visit_code is CollectionVisitCode.NO_MONEY
