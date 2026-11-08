"""ch1: client_chat unique key, integration flags, revision chain."""
import uuid

import pytest
from _mi_helpers import load
from conftest import CO_A
from sqlalchemy.exc import IntegrityError

from database_utils.models import ClientChat, Integration


def _chat(client, integ, address="5551", **kw):
    return ClientChat(company_id=CO_A, client_id=client, integration_id=integ, address=address, **kw)


def test_integration_flag_defaults(db):
    i = Integration(company_id=CO_A, name="n", base_url="u")
    db.add(i)
    db.commit()
    assert i.is_default is False and i.menu_enabled is True


def test_unique_per_client_integration_address(db):
    c, i = uuid.uuid4(), uuid.uuid4()
    db.add(_chat(c, i))
    db.commit()
    db.add(_chat(c, i))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # Household: same chat, another client; same client, another address.
    db.add_all([_chat(uuid.uuid4(), i), _chat(c, i, address="5552")])
    db.commit()


def test_revision_chain():
    m = load("versions/ch1_client_chat.py", "ch1_client_chat")
    assert (m.revision, m.down_revision) == ("ch1_client_chat", "ii1_integration_interest")
