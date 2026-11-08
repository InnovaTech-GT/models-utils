"""ii1: integration_interest unique key (company, type, provider-or-empty)."""
import uuid

import pytest
from _mi_helpers import load
from conftest import CO_A
from sqlalchemy.exc import IntegrityError

from database_utils.models import IntegrationInterest


def _row(provider, type_="PAYMENTS", **kw):
    return IntegrationInterest(company_id=kw.pop("company_id", CO_A), type=type_, provider=provider, **kw)


def test_duplicate_provider_conflicts(db):
    db.add(_row("STRIPE"))
    db.commit()
    db.add(_row("STRIPE"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_two_suggestions_of_one_type_conflict(db):
    db.add(_row(None, note="a"))
    db.commit()
    db.add(_row(None, note="b"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_provider_and_null_provider_coexist(db):
    db.add_all([_row("STRIPE"), _row(None), _row(None, type_="EMAIL"), _row("STRIPE", company_id=uuid.uuid4())])
    db.commit()


def test_revision_chain():
    m = load("versions/ii1_integration_interest.py", "ii1_integration_interest")
    assert (m.revision, m.down_revision) == ("ii1_integration_interest", "wf2_conversational")
