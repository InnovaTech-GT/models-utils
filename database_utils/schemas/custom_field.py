# schemas/custom_field.py
from pydantic import BaseModel, field_validator, ConfigDict
from typing import Optional
from uuid import UUID
from datetime import datetime
from database_utils.models.crm import CustomFieldType


def _validate_field_key(v: Optional[str]) -> Optional[str]:
    """field_key must be a valid identifier (alphanumeric and underscores)."""
    if v is not None:
        if not v.replace('_', '').isalnum():
            raise ValueError('field_key must contain only alphanumeric characters and underscores')
        return v.lower()
    return v


class CustomFieldDefinitionBase(BaseModel):
    field_name: str
    field_key: str
    field_type: CustomFieldType
    is_required: bool = False
    display_order: int = 0


class CustomFieldDefinitionCreate(CustomFieldDefinitionBase):
    """Schema for creating a custom field definition. Company ID is extracted from JWT token."""

    @field_validator('field_key')
    @classmethod
    def validate_field_key(cls, v: str) -> str:
        return _validate_field_key(v)


class CustomFieldDefinitionCreateInternal(CustomFieldDefinitionBase):
    """Internal schema with company_id for database operations."""
    company_id: UUID

    @field_validator('field_key')
    @classmethod
    def validate_field_key(cls, v: str) -> str:
        return _validate_field_key(v)


class CustomFieldDefinitionUpdate(BaseModel):
    field_name: Optional[str] = None
    field_key: Optional[str] = None
    field_type: Optional[CustomFieldType] = None
    is_required: Optional[bool] = None
    display_order: Optional[int] = None

    @field_validator('field_key')
    @classmethod
    def validate_field_key(cls, v: Optional[str]) -> Optional[str]:
        return _validate_field_key(v)


class CustomFieldDefinitionOut(CustomFieldDefinitionBase):
    id: UUID
    created_at: datetime
    company_id: UUID

    model_config = ConfigDict(from_attributes=True)


class ClientCustomFieldValueBase(BaseModel):
    value: Optional[str] = None


class ClientCustomFieldValueCreate(ClientCustomFieldValueBase):
    client_id: UUID
    field_definition_id: UUID


class ClientCustomFieldValueUpdate(BaseModel):
    value: Optional[str] = None


class ClientCustomFieldValueOut(ClientCustomFieldValueBase):
    id: UUID
    created_at: datetime
    client_id: UUID
    field_definition_id: UUID
    field_definition: Optional[CustomFieldDefinitionOut] = None

    model_config = ConfigDict(from_attributes=True)


# For use in client create/update - simplified version
class ClientCustomFieldValueInput(BaseModel):
    field_definition_id: UUID
    value: Optional[str] = None
