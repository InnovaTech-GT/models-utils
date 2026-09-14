# database_utils/dependencies/auth.py
"""
FastAPI dependencies for authentication and authorization.

This module provides reusable dependency functions for:
- Token extraction and validation
- User authentication
- Role-based access control (RBAC)
- Admin and super admin verification
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Callable, TYPE_CHECKING

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database_utils.dependencies.db import get_db

if TYPE_CHECKING:
    from database_utils.models.auth import User

# Configure module logger
logger = logging.getLogger(__name__)


def get_token_from_header(request: Request) -> str:
    """
    Extract JWT token from Authorization header.

    Args:
        request: FastAPI Request object

    Returns:
        str: JWT token extracted from Bearer scheme

    Raises:
        HTTPException: If Authorization header is missing or invalid

    Example:
        Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
    """
    logger.debug(
        "Extracting token from Authorization header",
        extra={
            "endpoint": request.url.path,
            "method": request.method,
            "client_host": request.client.host if request.client else None
        }
    )

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        logger.warning(
            "Missing Authorization header",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "client_host": request.client.host if request.client else None
            }
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )

    try:
        # Extract the token from "Bearer <token>"
        scheme, token = auth_header.split()
        if scheme.lower() != "bearer":
            logger.warning(
                "Invalid authentication scheme",
                extra={
                    "scheme": scheme,
                    "endpoint": request.url.path
                }
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme"
            )

        logger.debug(
            "Token extracted successfully",
            extra={
                "endpoint": request.url.path,
                "token_length": len(token)
            }
        )
        return token

    except ValueError as e:
        logger.error(
            "Invalid authorization header format",
            extra={
                "endpoint": request.url.path,
                "error": str(e)
            }
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
) -> "User":
    """
    Dependency to get the current authenticated user.

    Validates the JWT token and retrieves the user from the database.

    Args:
        request: FastAPI Request object
        db: Database session (injected via dependency)

    Returns:
        User: Authenticated user object

    Raises:
        HTTPException: If token is invalid or user not found
    """
    from database_utils.utils.jwt_utils import decode_token
    from database_utils.models.auth import User

    logger.info(
        "Authenticating user",
        extra={
            "endpoint": request.url.path,
            "method": request.method
        }
    )

    token = get_token_from_header(request)

    try:
        payload = decode_token(token)
        logger.debug(
            "Token decoded successfully",
            extra={
                "user_id": payload.get("id"),
                "roles": payload.get("roles"),
                "company_id": payload.get("company_id"),
                "is_super_admin": payload.get("is_super_admin", False)
            }
        )

        user_id_str = payload.get("id")
        if user_id_str is None:
            logger.error("Token payload missing user ID")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )

        # Convert string UUID back to UUID object for database query
        try:
            user_id = uuid.UUID(user_id_str)
        except (ValueError, AttributeError):
            logger.error(f"Invalid UUID format in token: {user_id_str}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user ID format in token"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Token validation failed",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "endpoint": request.url.path
            }
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}"
        )

    # Retrieve user from database
    try:
        user = db.query(User).filter(User.id == user_id).first()

        if user is None:
            logger.warning(
                "User not found in database",
                extra={
                    "user_id": user_id,
                    "endpoint": request.url.path
                }
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Check if user is active
        if not user.active:
            logger.warning(
                "Inactive user attempted to access resource",
                extra={
                    "user_id": user.id,
                    "email": user.email,
                    "endpoint": request.url.path
                }
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deactivated"
            )

        logger.info(
            "User authenticated successfully",
            extra={
                "user_id": user.id,
                "username": user.name,
                "roles": [role.name for role in user.roles],
                "company_id": user.company_id,
                "endpoint": request.url.path
            }
        )

        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Database error during user lookup",
            extra={
                "user_id": user_id,
                "error": str(e),
                "error_type": type(e).__name__
            }
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving user"
        )


async def get_admin_user(
    user: "User" = Depends(get_current_user),
) -> "User":
    """
    Dependency to verify the current user has admin privileges.

    Checks if the user has the ADMIN role in their roles relationship.

    Args:
        user: Current authenticated user (injected via dependency)

    Returns:
        User: The admin user object

    Raises:
        HTTPException: If user does not have admin privileges
    """
    # Get user's role names from many-to-many relationship
    user_role_names = [role.name for role in user.roles]
    has_admin_role = "ADMIN" in user_role_names

    logger.info(
        "Verifying admin privileges",
        extra={
            "user_id": user.id,
            "username": user.name,
            "roles": user_role_names,
            "has_admin_role": has_admin_role
        }
    )

    if not has_admin_role:
        logger.warning(
            "Admin access denied - insufficient privileges",
            extra={
                "user_id": user.id,
                "username": user.name,
                "roles": user_role_names
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )

    logger.info(
        "Admin privileges verified",
        extra={
            "user_id": user.id,
            "username": user.name
        }
    )

    return user


async def get_super_admin(
    user: "User" = Depends(get_current_user),
) -> "User":
    """
    Dependency to verify the current user is a super admin.

    Super admins must have:
    - is_super_admin=True
    - company_id=None (not associated with any company)

    Args:
        user: Current authenticated user (injected via dependency)

    Returns:
        User: The super admin user object

    Raises:
        HTTPException: If user is not a super admin
    """
    logger.info(
        "Verifying super admin privileges",
        extra={
            "user_id": user.id,
            "username": user.name,
            "is_super_admin": getattr(user, 'is_super_admin', False),
            "company_id": user.company_id
        }
    )

    if not getattr(user, 'is_super_admin', False):
        logger.warning(
            "Super admin access denied - not a super admin",
            extra={
                "user_id": user.id,
                "username": user.name
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin privileges required"
        )

    if user.company_id is not None:
        logger.warning(
            "Super admin access denied - user associated with company",
            extra={
                "user_id": user.id,
                "username": user.name,
                "company_id": user.company_id
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin must not be associated with any company"
        )

    logger.info(
        "Super admin privileges verified",
        extra={
            "user_id": user.id,
            "username": user.name
        }
    )

    return user


def require_roles(allowed_roles: List[str]) -> Callable:
    """
    Dependency factory to enforce role-based access control (RBAC).

    Creates a dependency that verifies the current user has one of the allowed roles.

    Args:
        allowed_roles: List of role names that are allowed to access the endpoint

    Returns:
        Callable: Async function that can be used as a FastAPI dependency

    Raises:
        HTTPException: If user's role is not in the allowed roles list

    Example:
        from database_utils.constants.roles import Roles

        @app.get("/admin-only")
        async def admin_endpoint(user: User = Depends(require_roles([Roles.ADMIN]))):
            return {"message": "Admin access granted"}
    """
    logger.debug(
        "Creating role checker dependency",
        extra={"allowed_roles": allowed_roles}
    )

    async def role_checker(user: "User" = Depends(get_current_user)) -> "User":
        """
        Inner dependency function that checks user role.

        Args:
            user: Current authenticated user

        Returns:
            User: The authenticated user if role check passes

        Raises:
            HTTPException: If user role not in allowed_roles
        """
        # Get user's role names from many-to-many relationship
        user_role_names = [role.name for role in user.roles]

        logger.info(
            "Checking user roles against allowed roles",
            extra={
                "user_id": user.id,
                "username": user.name,
                "user_roles": user_role_names,
                "allowed_roles": allowed_roles
            }
        )

        # Check if user has any of the allowed roles
        has_allowed_role = any(role_name in allowed_roles for role_name in user_role_names)

        if not has_allowed_role:
            logger.warning(
                "Access denied - insufficient role permissions",
                extra={
                    "user_id": user.id,
                    "username": user.name,
                    "user_roles": user_role_names,
                    "allowed_roles": allowed_roles
                }
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource"
            )

        logger.info(
            "Role check passed",
            extra={
                "user_id": user.id,
                "username": user.name,
                "user_roles": user_role_names
            }
        )

        return user

    return role_checker
