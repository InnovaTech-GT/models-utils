from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any, Literal
from uuid import UUID
from datetime import datetime

from database_utils.models.crm import IntegrationAuthType

MASKED = "***"
IntegrationProvider = Literal["WHATSAPP_BUSINESS"]
SENSITIVE_KEYS = {"api_key", "token", "password"}


class IntegrationCreate(BaseModel):
    name: str
    description: Optional[str] = None
    base_url: str
    auth_type: IntegrationAuthType = IntegrationAuthType.NONE
    credentials: Optional[Dict[str, Any]] = None
    provider: Optional[IntegrationProvider] = None
    enabled: bool = True


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[IntegrationAuthType] = None
    credentials: Optional[Dict[str, Any]] = None
    # None = unchanged (router uses exclude_unset/None-skip semantics)
    provider: Optional[IntegrationProvider] = None
    enabled: Optional[bool] = None


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
