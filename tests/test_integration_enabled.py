"""fg1: a disabled integration is never called out with by workflow HTTP_REQUEST."""
import sys
import uuid
from types import SimpleNamespace

import pytest

from database_utils.models.crm import Integration
from database_utils.schemas.integration import IntegrationOut, IntegrationUpdate
from database_utils.utils.workflow_engine import _execute_http_request

from conftest import CO_A


def test_http_request_refuses_disabled_integration(db, monkeypatch):
    # httpx is lazily imported and lives in backend-erp, not here; the guard
    # must fire before it is ever used.
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace())
    integ = Integration(
        id=uuid.uuid4(), company_id=CO_A, name="TEST", base_url="https://example.com",
        enabled=False,
    )
    db.add(integ)
    db.commit()
    step = SimpleNamespace(action_config={"integration_id": integ.id, "method": "GET"})
    with pytest.raises(ValueError, match="is disabled"):
        _execute_http_request(db, step, {}, CO_A)


def test_integration_defaults_and_schema_round_trip(db):
    integ = Integration(id=uuid.uuid4(), company_id=CO_A, name="TEST", base_url="https://x")
    db.add(integ)
    db.commit()
    out = IntegrationOut.from_orm_masked(integ)
    assert out.enabled is True and out.provider is None
    # None = unchanged: unset fields don't appear in the PATCH dump
    assert IntegrationUpdate(enabled=False).model_dump(exclude_unset=True) == {"enabled": False}
    with pytest.raises(ValueError):
        IntegrationUpdate(provider="SLACK")


def test_whatsapp_app_secret_is_masked(db):
    integ = Integration(
        id=uuid.uuid4(), company_id=CO_A, name="WA", base_url="https://x",
        provider="WHATSAPP_BUSINESS",
        credentials={"token": "t", "app_secret": "s", "verify_token": "v", "phone_number_id": "1"},
    )
    db.add(integ)
    db.commit()
    creds = IntegrationOut.from_orm_masked(integ).credentials
    assert creds["token"] == "***" and creds["app_secret"] == "***"
    # verify_token no es secreto: el tenant necesita verlo para configurar Meta
    assert creds["verify_token"] == "v" and creds["phone_number_id"] == "1"
