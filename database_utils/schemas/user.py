# schemas/user.py
from datetime import datetime

from pydantic import BaseModel, EmailStr, constr, ConfigDict
from typing import Optional, List
from uuid import UUID


class RoleSimple(BaseModel):
    """Simple role schema for user responses"""
    id: UUID
    name: str

    model_config = ConfigDict(from_attributes=True)


class UserBase(BaseModel):
    """Base user schema without legacy role/admin fields.

    `age` is legacy dead weight (signup and invitation-accept write 0, nothing
    reads it) awaiting a drop migration. It stays required so `UserOut` keeps
    carrying it, but no UI may surface it — never send it, never render it.
    """
    name: str
    email: EmailStr
    age: int
    phone: Optional[str] = None
    photo_url: Optional[str] = None


class UserCreate(UserBase):
    """Schema for creating a new user with role assignments"""
    password: constr(min_length=6)
    company_id: Optional[UUID] = None
    role_ids: Optional[List[UUID]] = []


class UserUpdate(BaseModel):
    """Schema for updating user information"""
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    age: Optional[int] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    password: Optional[constr(min_length=6)] = None
    company_id: Optional[UUID] = None
    role_ids: Optional[List[UUID]] = None


class UserSelfUpdate(BaseModel):
    """Schema for a user editing their OWN profile (auth-erp `PATCH /me`).

    Deliberately narrower than `UserUpdate`: no `age` (legacy), no `password`
    (no current-password check exists), no `company_id`, no `role_ids` — a
    self-edit can never be a privilege escalation.
    """
    name: Optional[constr(min_length=2, max_length=255)] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None


class UserOut(UserBase):
    """Output schema for user information"""
    id: UUID
    created_at: datetime
    company_id: Optional[UUID] = None
    is_super_admin: Optional[bool] = False
    active: bool = True

    model_config = ConfigDict(from_attributes=True)


class UserWithRoles(UserOut):
    """Extended user model with role details"""
    roles: List[RoleSimple] = []

    model_config = ConfigDict(from_attributes=True)


class SuperAdminCreate(BaseModel):
    """Schema for creating a super admin user (no company_id required)"""
    name: str
    email: EmailStr
    age: int
    password: constr(min_length=6)
    is_super_admin: Optional[bool] = True
