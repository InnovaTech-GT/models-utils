# schemas/password_reset.py
from pydantic import BaseModel, EmailStr, constr
from typing import Literal, Optional
from datetime import datetime


class PasswordResetRequestSchema(BaseModel):
    """Body for POST /password-reset/request."""
    email: EmailStr
    locale: Literal["es", "en"] = "es"


class PasswordResetValidate(BaseModel):
    """Response for GET /password-reset/validate/{token}."""
    token: str
    valid: bool
    email: Optional[str] = None
    expires_at: Optional[datetime] = None
    message: Optional[str] = None


class PasswordResetConfirmSchema(BaseModel):
    """Body for POST /password-reset/confirm."""
    token: str
    new_password: constr(min_length=8)
