# schemas/audit_log.py
from pydantic import BaseModel, ConfigDict
from typing import Optional, Any
from datetime import datetime
from uuid import UUID


class AuditLogBase(BaseModel):
    """Base schema for AuditLog"""
    action: str
    resource_type: str
    resource_id: Optional[str] = None  # Changed to string to store UUID
    details: Optional[dict[str, Any]] = None
    ip_address: Optional[str] = None


class AuditLogOut(AuditLogBase):
    """Schema for audit log output"""
    id: UUID
    created_at: datetime
    user_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)
