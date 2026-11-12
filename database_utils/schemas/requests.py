from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


# --- Login ---
class LoginRequest(BaseModel):
    usuario_id: EmailStr
    password: str
    # mi2 / auth hardening: 'mobile' gets a short access token
    # (MOBILE_ACCESS_TOKEN_EXPIRE) and a refresh token tagged cl="m".
    client_type: Literal["web", "mobile"] = "web"


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
    # Doc 69 §4.1: a referral code needs tax_id + phone (auth-erp enforces
    # REFERRAL_IDENTITY_REQUIRED / NIT_INVALID and normalises the code).
    referral_code: Optional[str] = Field(None, max_length=12)
    tax_id: Optional[str] = Field(None, max_length=32)
    phone: Optional[str] = Field(None, max_length=32)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., description="A valid refresh token previously issued")

    class Config:
        schema_extra = {
            "example": {
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
            }
        }
