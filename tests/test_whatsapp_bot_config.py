import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import WhatsAppBotConfig
from database_utils.schemas.whatsapp_bot import (
    BUILTIN_FLOWS, WhatsAppBotConfigIn, default_whatsapp_bot_config,
)

from conftest import CO_A


def _config(options, **extra):
    return {"welcome_text": "Hola", "options": options, **extra}


def _info(i="horarios", **kw):
    return {"id": i, "title": "Horarios", "type": "INFO", "text": "8 a 18", **kw}


def test_menu_predeterminado_es_valido_y_trae_los_flujos_actuales():
    cfg = default_whatsapp_bot_config()
    assert [o.flow for o in cfg.options if o.type == "BUILTIN"] == list(BUILTIN_FLOWS)
    assert [o.purpose for o in cfg.options if o.type == "PURPOSE"] == ["WIFI_PASSWORD", "LAN_HOSTS", "TRAFFIC_USAGE"]
    assert len(cfg.options) == 9
    assert cfg.enabled is True and cfg.menu_button_text == "Ver opciones"


def test_acepta_los_cuatro_tipos():
    cfg = WhatsAppBotConfigIn(**_config([
        _info(),
        {"id": "wifi", "title": "Cambiar clave WiFi", "type": "CREATE_TASK",
         "job_kind": "MAINTENANCE", "task_name": "Cambio de clave WiFi"},
        {"id": "falla", "title": "Reportar una falla", "type": "BUILTIN", "flow": "falla"},
        {"id": "hosts", "title": "Quién está conectado", "type": "PURPOSE", "purpose": "LAN_HOSTS"},
    ]))
    assert [o.type for o in cfg.options] == ["INFO", "CREATE_TASK", "BUILTIN", "PURPOSE"]
    assert cfg.options[1].requires_client is True


@pytest.mark.parametrize("opciones", [
    [],                                                           # sin opciones
    [_info(f"o{i}") for i in range(11)],                          # mas de 10
    [_info("x"), _info("x")],                                     # ids repetidos
    [_info(title="Un título demasiado largo!!")],                 # > 24
    [_info(i="Con Espacios")],                                    # id invalido
    [{"id": "x", "title": "X", "type": "WORKFLOW"}],              # tipo desconocido
    [{"id": "x", "title": "X", "type": "CREATE_TASK", "job_kind": "OTRO", "task_name": "t"}],
    [{"id": "x", "title": "X", "type": "BUILTIN", "flow": "otro"}],
    [_info(extra="no permitido")],                                # campo extra
    [{"id": "x", "title": "X", "type": "PURPOSE", "purpose": "ACTIVATION"}],  # canonico
    [{"id": "x", "title": "X", "type": "PURPOSE", "purpose": "wifi"}],        # minusculas
    [{"id": "x", "title": "X", "type": "PURPOSE"}],                           # sin proposito
])
def test_rechaza_configs_invalidas(opciones):
    with pytest.raises(ValidationError):
        WhatsAppBotConfigIn(**_config(opciones))


def test_boton_maximo_20_caracteres():
    with pytest.raises(ValidationError):
        WhatsAppBotConfigIn(**_config([_info()], menu_button_text="x" * 21))


def test_una_sola_config_por_cablera(db):
    opciones = default_whatsapp_bot_config().model_dump()["options"]
    db.add(WhatsAppBotConfig(id=uuid.uuid4(), company_id=CO_A, welcome_text="Hola", options=opciones))
    db.commit()
    guardada = db.query(WhatsAppBotConfig).filter_by(company_id=CO_A).one()
    assert guardada.options[0]["flow"] == "aumento" and guardada.enabled is True

    db.add(WhatsAppBotConfig(id=uuid.uuid4(), company_id=CO_A, welcome_text="Otra", options=opciones))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
