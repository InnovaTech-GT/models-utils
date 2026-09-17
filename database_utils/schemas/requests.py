from typing import Literal

from pydantic import BaseModel, EmailStr, Field


# --- Login ---
class LoginRequest(BaseModel):
    usuario_id: EmailStr
    password: str


# --- Company Signup (company-only; users join by invitation) ---
class SignupCompanyRequest(BaseModel):
    """Body for POST /signup/company — flat minimal signup shape.

    Creates the company plus its first (admin) user. Regular users are
    invitation-only; there is no self-serve user signup.
    """
    company_name: str = Field(..., min_length=2, max_length=255)
    name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=8)
    locale: Literal["es", "en"] = "es"


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., description="A valid refresh token previously issued")

    class Config:
        schema_extra = {
            "example": {
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
            }
        }
