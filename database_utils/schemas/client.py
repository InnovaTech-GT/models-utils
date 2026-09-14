# schemas/client.py
from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Optional, List
from uuid import UUID

from database_utils.models.crm import ServiceAvailability

from .user import UserOut


class ClientBase(BaseModel):
    name: str
    tax_id: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    email: Optional[EmailStr]
    contact: Optional[str]
    observations: Optional[str]
    # ISP fields (ADR-004)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    gps_precision_m: Optional[float] = None
    service_availability: ServiceAvailability = ServiceAvailability.UNKNOWN


class ClientCreate(ClientBase):
    company_id: Optional[UUID] = None  # Optional - will be set from authenticated user context
    advisor_id: Optional[UUID] = None
    assigned_technician_id: Optional[UUID] = None
    custom_field_values: Optional[List["ClientCustomFieldValueInput"]] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    contact: Optional[str] = None
    observations: Optional[str] = None
    company_id: Optional[UUID] = None
    advisor_id: Optional[UUID] = None
    assigned_technician_id: Optional[UUID] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    gps_precision_m: Optional[float] = None
    service_availability: Optional[ServiceAvailability] = None
    custom_field_values: Optional[List["ClientCustomFieldValueInput"]] = None


class ClientOut(ClientBase):
    id: UUID
    company_id: UUID
    # Services summary rollup (cf1, replaces the dropped stored
    # installation_status): read-only, COMPUTED by backend-erp's clients
    # list/detail endpoints from client_service rows
    # (install_state='INSTALLED' for the second count) — never stored,
    # never on Create/Update. Defaults keep the schema valid for callers
    # that hydrate straight from the ORM row.
    services_total: int = 0
    services_installed: int = 0
    advisor_id: Optional[UUID]
    advisor: Optional[UserOut] = None
    assigned_technician_id: Optional[UUID] = None
    assigned_technician: Optional[UserOut] = None
    custom_field_values: Optional[List["ClientCustomFieldValueOut"]] = None

    model_config = ConfigDict(from_attributes=True)


# Import at the end to avoid circular imports
from .custom_field import ClientCustomFieldValueInput, ClientCustomFieldValueOut

# Rebuild model to resolve forward references
ClientCreate.model_rebuild()
ClientUpdate.model_rebuild()
ClientOut.model_rebuild()
