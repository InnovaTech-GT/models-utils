# utils/jwt_utils.py
import jwt
import os
import uuid
from datetime import timedelta
from typing import Optional
from fastapi import HTTPException
from database_utils.schemas.user import UserOut
from database_utils.utils.timezone_utils import now_gt
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# Load environment variables with defaults to avoid errors during import.
# NOTE: the env var is ACCESS_TOKEN_EXPIRE (minutes) — every service .env sets it.
# The old name ACCESS_TOKEN_EXPIRE_MINUTES was never set anywhere, so the 114400
# default (~79 days) was silently always in effect.
access_expire = int(os.getenv("ACCESS_TOKEN_EXPIRE", "1440"))  # 1 day in minutes
refresh_expire = int(os.getenv("REFRESH_TOKEN_EXPIRE", "604800"))  # SECONDS (7 days default; 2592000 = 30 days)
# Access-token TTL for the field apps (LoginRequest.client_type == "mobile"),
# in minutes. Short because the apps refresh; the web keeps access_expire.
mobile_access_expire = int(os.getenv("MOBILE_ACCESS_TOKEN_EXPIRE", "60"))

# `type` claim. Tokens issued before it existed have none: a legacy access
# token carries `roles`, a legacy refresh token carries only `id`.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# Signing key. Fail fast in production rather than silently falling back to a
# well-known string (which would allow token forgery). Dev/test keep a fallback.
secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    if os.getenv("ENVIRONMENT") == "production":
        raise RuntimeError("SECRET_KEY environment variable must be set in production")
    secret_key = "default-secret-key-for-development"

ALGORITHM = "HS256"

def create_token(
    data: dict,
    expires_delta: Optional[timedelta] = None
):
    to_encode = data.copy()
    expire = now_gt() + (expires_delta or timedelta(minutes=access_expire))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)


def create_access_token(usuario, expires_minutes: Optional[int] = None, sid=None):
    """
    Create an access token for a user.

    Args:
        usuario: User object (can be UserOut, User model, or any object with id, roles, company_id)
        expires_minutes: TTL override (mobile logins pass mobile_access_expire);
            None = ACCESS_TOKEN_EXPIRE.
        sid: session id = the refresh family_id (auth-erp issue_token_pair).
            Lets the auth deps reject the token once the family is revoked
            (utils/sessions.py). None = no `sid` claim (not revocable).

    Returns:
        str: Encoded JWT token
    """
    # Get role names from the many-to-many relationship
    # Support both Pydantic models and SQLAlchemy models
    role_names = []
    if hasattr(usuario, 'roles'):
        roles = getattr(usuario, 'roles', [])
        if roles:
            # Handle SQLAlchemy relationship or Pydantic list
            role_names = [role.name if hasattr(role, 'name') else role for role in roles]

    # Convert UUID to string for JSON serialization
    user_id = str(usuario.id) if usuario.id else None
    company_id = getattr(usuario, 'company_id', None)
    company_id = str(company_id) if company_id else None

    data = {
        "id": user_id,
        "roles": role_names,
        "company_id": company_id,
        "is_super_admin": getattr(usuario, 'is_super_admin', False),
        "type": TOKEN_TYPE_ACCESS,
    }
    if sid is not None:
        data["sid"] = str(sid)
    minutes = access_expire if expires_minutes is None else expires_minutes
    token = create_token(data, expires_delta=timedelta(minutes=minutes))
    logger.info(f"Access token created for user {user_id} with roles {role_names}")
    return token
    
def create_refresh_token(usuario: UserOut, client_type: str = "web", jti: Optional[str] = None):
    """`cl` remembers the client ("m" mobile / "w" web) so /refresh can issue
    the next access token with the same TTL. `jti` keys the server-side
    `auth_refresh_token` row (auth-erp passes the one it records; a fresh
    uuid4 hex otherwise)."""
    # Convert UUID to string for JSON serialization
    user_id = str(usuario.id) if usuario.id else None
    data = {
        "id": user_id,
        "type": TOKEN_TYPE_REFRESH,
        "cl": "m" if client_type == "mobile" else "w",
        "jti": jti or uuid.uuid4().hex,
    }
    token = create_token(data, expires_delta=timedelta(seconds=refresh_expire))
    logger.info(f"Refresh token created for user {user_id}")
    return token

def is_refresh_payload(payload: dict) -> bool:
    """True for a refresh token: `type == "refresh"`, or a legacy token (no
    `type`) that also has no `roles` — legacy access tokens always had one."""
    if "type" in payload:
        return payload["type"] == TOKEN_TYPE_REFRESH
    return "roles" not in payload


def decode_token(token: str):
    try:
        # Never log the payload or the token: both are bearer credentials.
        return jwt.decode(token, secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Signature has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Error: {e}")
    
