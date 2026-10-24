"""Menú configurable del bot de WhatsApp (Nova) por cablera.

Límites de WhatsApp para mensajes de lista: hasta 10 filas, título de fila
<= 24, descripción <= 72, botón <= 20, cuerpo <= 1024.
"""
from datetime import datetime
from typing import Annotated, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database_utils.models.crm import TaskJobKind
from database_utils.models.isp import CANONICAL_PLAYBOOK_PURPOSES, PLAYBOOK_PURPOSE_PATTERN

__all__ = [
    "BUILTIN_FLOWS", "WhatsAppBotOption", "InfoOption", "CreateTaskOption", "BuiltinOption", "PurposeOption",
    "WhatsAppBotConfigIn", "WhatsAppBotConfigOut", "default_whatsapp_bot_config",
]

MAX_OPTIONS = 10
BUILTIN_FLOWS = ("aumento", "falla", "factura", "instalacion", "agente", "resincronizar")
BuiltinFlow = Literal["aumento", "falla", "factura", "instalacion", "agente", "resincronizar"]
OptionId = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")]


class _OptionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: OptionId
    title: str = Field(min_length=1, max_length=24)
    description: Optional[str] = Field(default=None, max_length=72)


class InfoOption(_OptionBase):
    """Responde un texto fijo y vuelve al menú."""
    type: Literal["INFO"]
    text: str = Field(min_length=1, max_length=4096)


class CreateTaskOption(_OptionBase):
    """Crea una tarea del tipo elegido por la cablera."""
    type: Literal["CREATE_TASK"]
    job_kind: TaskJobKind
    task_name: str = Field(min_length=1, max_length=120)
    # true = solo para clientes verificados (número registrado o NIT/DPI).
    requires_client: bool = True
    confirmation_text: Optional[str] = Field(default=None, max_length=1024)


class BuiltinOption(_OptionBase):
    """Uno de los flujos que ya existen en el bot, renombrado/reordenado."""
    type: Literal["BUILTIN"]
    flow: BuiltinFlow


class PurposeOption(_OptionBase):
    """Corre un proposito personalizado de la cablera (WIFI_PASSWORD,
    LAN_HOSTS, ...) con el flujo generico del bot: el bot le pregunta al
    backend que datos pedir y que mostrar, asi que no sabe que hace cada uno.
    Los cuatro canonicos mueven estado y facturacion: son de oficina."""
    type: Literal["PURPOSE"]
    purpose: str = Field(pattern=PLAYBOOK_PURPOSE_PATTERN)

    @field_validator("purpose")
    @classmethod
    def _no_canonico(cls, v: str) -> str:
        if v in CANONICAL_PLAYBOOK_PURPOSES:
            raise ValueError(f"'{v}' es una accion de oficina, no del suscriptor")
        return v


WhatsAppBotOption = Annotated[
    Union[InfoOption, CreateTaskOption, BuiltinOption, PurposeOption], Field(discriminator="type")
]


class WhatsAppBotConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    welcome_text: str = Field(min_length=1, max_length=1024)
    menu_button_text: str = Field(default="Ver opciones", min_length=1, max_length=20)
    options: List[WhatsAppBotOption] = Field(min_length=1, max_length=MAX_OPTIONS)

    @model_validator(mode="after")
    def _ids_unicos(self):
        ids = [o.id for o in self.options]
        repetidos = sorted({i for i in ids if ids.count(i) > 1})
        if repetidos:
            raise ValueError(f"ids de opción repetidos: {', '.join(repetidos)}")
        return self


class WhatsAppBotConfigOut(WhatsAppBotConfigIn):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[UUID] = None          # None = menú predeterminado (sin fila guardada)
    company_id: Optional[UUID] = None
    updated_at: Optional[datetime] = None


def default_whatsapp_bot_config() -> WhatsAppBotConfigIn:
    """El menú que el bot usa hoy, para cablera sin config y para 'Restaurar'."""
    titulos = {
        "aumento": "Aumentar mi plan", "falla": "Reportar una falla",
        "factura": "Consultar mi factura", "instalacion": "Nueva instalación",
        "agente": "Hablar con un agente", "resincronizar": "Resincronizar mi equipo",
    }
    # Los casos que el bot corre con un playbook de la cablera. Sin ese
    # playbook el bot responde que la opcion no esta disponible.
    propositos = [
        ("wifi_password", "Cambiar mi clave WiFi", "WIFI_PASSWORD"),
        ("lan_hosts", "Quién está conectado", "LAN_HOSTS"),
        ("traffic_usage", "Ver mi consumo", "TRAFFIC_USAGE"),
    ]
    return WhatsAppBotConfigIn(
        welcome_text="¡Hola! 👋 Soy *Nova*, tu asistente virtual.\n¿En qué te puedo ayudar hoy?",
        options=[{"id": f, "title": titulos[f], "type": "BUILTIN", "flow": f} for f in BUILTIN_FLOWS]
        + [{"id": i, "title": t, "type": "PURPOSE", "purpose": pr} for i, t, pr in propositos],
    )
