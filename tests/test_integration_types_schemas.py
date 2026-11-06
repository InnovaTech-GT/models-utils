"""doc 65 integration schemas: interest in/out, is_default, menu_enabled, chat out."""
import uuid
from datetime import datetime, timezone

import pytest
from conftest import CO_A
from pydantic import ValidationError

from database_utils.models.crm import Integration
from database_utils.schemas.integration import (
    ClientChatOut,
    IntegrationCreate,
    IntegrationInterestIn,
    IntegrationOut,
    IntegrationUpdate,
)


def test_interest_provider_only_ok():
    assert IntegrationInterestIn(type="PAYMENTS", provider="STRIPE").note is None


def test_interest_type_and_note_ok():
    assert IntegrationInterestIn(type="EMAIL", note="Mailgun please").provider is None


@pytest.mark.parametrize("kw", [
    {"type": "EMAIL"}, {"type": "EMAIL", "note": "   "}, {"type": "SMS", "provider": "X"},
    {"type": "EMAIL", "note": "x" * 501}, {"type": "EMAIL", "provider": "x" * 33},
    {"type": "EMAIL", "provider": "X", "extra": 1},
])
def test_interest_rejected(kw):
    with pytest.raises(ValidationError):
        IntegrationInterestIn(**kw)


def test_integration_out_is_default_and_menu(db):
    integ = Integration(id=uuid.uuid4(), company_id=CO_A, name="T", base_url="https://x")
    db.add(integ)
    db.commit()
    out = IntegrationOut.from_orm_masked(integ)
    assert out.is_default is False and out.menu_enabled is True
    integ.is_default = True
    integ.menu_enabled = False
    db.commit()
    out = IntegrationOut.from_orm_masked(integ)
    assert out.is_default is True and out.menu_enabled is False


def test_update_and_create_flags():
    assert IntegrationUpdate(is_default=True).model_dump(exclude_unset=True) == {"is_default": True}
    assert IntegrationUpdate(menu_enabled=False).model_dump(exclude_unset=True) == {"menu_enabled": False}
    assert IntegrationCreate(name="n", base_url="u").menu_enabled is None


def test_client_chat_out_hides_address():
    assert "address" not in ClientChatOut.model_fields
    o = ClientChatOut(integration_id=uuid.uuid4(), provider="TELEGRAM_BOT", username="juan",
                      verified_at=datetime.now(timezone.utc))
    assert o.last_inbound_at is None
