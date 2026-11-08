"""TELEGRAM_BOT como proveedor de integracion (6.25.0)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from database_utils.models.crm import IntegrationAuthType
from database_utils.schemas.integration import (
    MASKED,
    IntegrationCreate,
    IntegrationOut,
    IntegrationUpdate,
)


def _fila(**credenciales):
    return SimpleNamespace(
        id=uuid4(), company_id=uuid4(), name="Telegram", description=None,
        base_url="https://api.telegram.org", auth_type=IntegrationAuthType.NONE,
        credentials=credenciales, provider="TELEGRAM_BOT", enabled=True,
        created_at=datetime.now(timezone.utc), updated_at=None,
    )


def test_create_acepta_telegram():
    body = IntegrationCreate(name="Telegram", base_url="https://api.telegram.org",
                             provider="TELEGRAM_BOT")
    assert body.provider == "TELEGRAM_BOT"


def test_update_acepta_telegram():
    assert IntegrationUpdate(provider="TELEGRAM_BOT").provider == "TELEGRAM_BOT"


@pytest.mark.parametrize("proveedor", ["TELEGRAM", "telegram_bot", "SIGNAL"])
def test_proveedor_desconocido(proveedor):
    with pytest.raises(ValidationError):
        IntegrationCreate(name="x", base_url="https://x.test", provider=proveedor)


def test_masked_tapa_token_y_secreto():
    out = IntegrationOut.from_orm_masked(_fila(
        token="123:token-de-prueba", webhook_secret="secreto-de-prueba",
        bot_username="cablera_bot", bot_id=123,
    ))
    assert out.credentials["token"] == MASKED
    assert out.credentials["webhook_secret"] == MASKED
    assert out.credentials["bot_username"] == "cablera_bot"
    assert out.credentials["bot_id"] == 123
