# utils/jwt_utils.py
import jwt
import os
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
refresh_expire = int(os.getenv("REFRESH_TOKEN_EXPIRE", "604800"))  # 7 days in seconds

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


def create_access_token(usuario):
    """
    Create an access token for a user.

    Args:
        usuario: User object (can be UserOut, User model, or any object with id, roles, company_id)

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
        "is_super_admin": getattr(usuario, 'is_super_admin', False)
    }
    token = create_token(data, expires_delta=timedelta(minutes=access_expire))
    logger.info(f"Access token created for user {user_id} with roles {role_names}")
    return token
    
def create_refresh_token(usuario: UserOut):
    # Convert UUID to string for JSON serialization
    user_id = str(usuario.id) if usuario.id else None
    data = {"id": user_id}
    token = create_token(data, expires_delta=timedelta(seconds=refresh_expire))
    logger.info(f"Refresh token created for user {user_id}")
    return token

def decode_token(token: str):
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        logger.info(f"{payload = }")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Signature has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Error: {e}")
    
