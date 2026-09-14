from pydantic import BaseModel, EmailStr, ConfigDict, constr
from typing import Literal, Optional, List
from datetime import datetime
from uuid import UUID


class InvitationCreate(BaseModel):
    """Schema for creating a user invitation"""
    email: EmailStr
    name: Optional[str] = None
    role_ids: List[UUID] = []
    locale: Literal["es", "en"] = "es"


class InvitationOut(BaseModel):
    """Schema for returning invitation data"""
    id: UUID
    created_at: datetime
    expires_at: datetime
    email: str
    name: Optional[str]
    status: str
    company_id: UUID
    invited_by_user_id: Optional[UUID]

    model_config = ConfigDict(from_attributes=True)


class InvitationAccept(BaseModel):
    """Schema for accepting an invitation"""
    token: str
    name: str
    password: constr(min_length=8)


class InvitationValidate(BaseModel):
    """Schema for validating an invitation token"""
    token: str
    valid: bool
    email: Optional[str] = None
    name: Optional[str] = None  # inviter-provided display name, prefills the accept form
    company_name: Optional[str] = None
    expires_at: Optional[datetime] = None
    message: Optional[str] = None
