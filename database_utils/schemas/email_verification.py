# schemas/email_verification.py
from pydantic import BaseModel, EmailStr
from typing import Literal, Optional
from datetime import datetime


class ConfirmEmailRequest(BaseModel):
    """Body for POST /confirm-email — exchanges a valid token for access/refresh tokens."""
    token: str


class ConfirmEmailValidate(BaseModel):
    """Response for GET /confirm-email/validate/{token}."""
    token: str
    valid: bool
    email: Optional[str] = None
    expires_at: Optional[datetime] = None
    message: Optional[str] = None


class ResendConfirmationRequest(BaseModel):
    """Body for POST /confirm-email/resend."""
    email: EmailStr
    locale: Literal["es", "en"] = "es"
