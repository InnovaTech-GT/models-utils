from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, Dict, Any, Literal
from uuid import UUID
from datetime import datetime

from database_utils.models.crm import IntegrationAuthType

MASKED = "***"
IntegrationProvider = Literal["WHATSAPP_BUSINESS", "RECURRENTE", "TELEGRAM_BOT"]
IntegrationType = Literal["CONVERSATION", "PAYMENTS", "EMAIL", "HTTP"]
# app_secret: App Secret de Meta (WhatsApp Business); firma los webhooks del tenant.
# webhook_secret: signingSecret (Svix) del webhook de Recurrente, o el secret_token
#   del webhook de Telegram. Lo pone el servidor en los dos casos.
# token: el de Meta (WhatsApp) o el del bot de Telegram (BotFather).
SENSITIVE_KEYS = {"api_key", "token", "password", "app_secret", "webhook_secret"}


class IntegrationCreate(BaseModel):
    name: str
    description: Optional[str] = None
    base_url: str
    auth_type: IntegrationAuthType = IntegrationAuthType.NONE
    credentials: Optional[Dict[str, Any]] = None
    provider: Optional[IntegrationProvider] = None
    enabled: bool = True
    # None = true. false = the channel stays silent for unsolicited messages (D38).
    menu_enabled: Optional[bool] = None


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[IntegrationAuthType] = None
    credentials: Optional[Dict[str, Any]] = None
    # None = unchanged (router uses exclude_unset/None-skip semantics)
    provider: Optional[IntegrationProvider] = None
    enabled: Optional[bool] = None
    menu_enabled: Optional[bool] = None
    # True = "Usar como predeterminada" (the service clears the other rows of the type).
    is_default: Optional[bool] = None


class IntegrationOut(BaseModel):
    id: UUID
    company_id: UUID
    name: str
    description: Optional[str] = None
    base_url: str
    auth_type: IntegrationAuthType
    credentials: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    enabled: bool = True
    menu_enabled: bool = True
    is_default: bool = False  # raw flag; the backend computes the effective default
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_masked(cls, obj) -> "IntegrationOut":
        """Return an IntegrationOut with sensitive credential values replaced by ***."""
        instance = cls.model_validate(obj)
        if instance.credentials:
            instance.credentials = {
                k: MASKED if k in SENSITIVE_KEYS else v
                for k, v in instance.credentials.items()
            }
        return instance


class IntegrationInterestIn(BaseModel):
    """"Me interesa" (provider set) or "Sugerir" (provider None + a note)."""
    model_config = ConfigDict(extra="forbid")

    type: IntegrationType
    provider: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _provider_or_note(self):
        if not self.provider and not (self.note and self.note.strip()):
            raise ValueError("provider or a non-blank note is required")
        return self


class IntegrationInterestOut(BaseModel):
    id: UUID
    company_id: UUID
    company_name: Optional[str] = None
    user_name: Optional[str] = None
    provider: Optional[str] = None
    type: str
    note: Optional[str] = None
    created_at: datetime


class ClientChatOut(BaseModel):
    """A client's linked chat. No address: opaque chat_id on Telegram, and the
    profile already has the phone for WhatsApp."""
    model_config = ConfigDict(from_attributes=True)

    integration_id: UUID
    provider: Optional[str] = None
    username: Optional[str] = None
    verified_at: Optional[datetime] = None
    last_inbound_at: Optional[datetime] = None
