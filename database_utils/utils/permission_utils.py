# utils/permission_utils.py
from __future__ import annotations

from typing import List, Set, Optional, TYPE_CHECKING
from uuid import UUID
from sqlalchemy.orm import Session
from fastapi import HTTPException, status, Request, Depends
from database_utils.dependencies.auth import get_token_from_header
from database_utils.utils.provisioning_gates import playbooks_self_managed  # noqa: F401 (re-exported)

if TYPE_CHECKING:
    from database_utils.models.auth import User


# doc 70 §4: the four names a company Uplink manages may not use, whatever its
# roles grant (role rows are never touched; they come back on approval).
MANAGED_PERMISSIONS = frozenset({"playbooks.create", "playbooks.read", "playbooks.update", "playbooks.delete"})
PLAYBOOKS_MANAGED_DETAIL = {"code": "PLAYBOOKS_MANAGED",
                            "message": "Uplink administra los playbooks de esta empresa."}


def denied_permissions(user) -> frozenset:
    """Permissions the user's roles may grant but the company may not use (doc 70 §4)."""
    # getattr: duck-typed users (tests, service principals) without the ORM
    # columns read as company-less; every real User row has both.
    if getattr(user, "is_staff_actor", False) or getattr(user, "company_id", None) is None:
        return frozenset()
    return frozenset() if playbooks_self_managed(getattr(user, "company", None)) else MANAGED_PERMISSIONS


def _overlay_denies(denied: frozenset, permission_name: str) -> bool:
    return permission_name in denied or (permission_name == "playbooks.*" and bool(denied))


class PermissionChecker:
    """Utility class for checking user permissions"""

    @staticmethod
    def get_user_permissions(user: User) -> Set[str]:
        """
        Get all permission names for a user from their roles.
        Returns a set of permission names like {'clients.read', 'orders.create', ...}
        """
        permissions = set()

        # Collect permissions from all user roles
        for role in user.roles:
            # The built-in ADMIN role gets all permissions (wildcard). Only the
            # global role counts — a tenant custom role named "ADMIN" must not.
            # A wildcard cannot be subtracted inside a set: callers learn the
            # overlay's exceptions from denied_permissions (doc 70 §4).
            if role.name == "ADMIN" and role.company_id is None:
                return {'*'}  # Special wildcard permission

            for permission in role.permissions:
                permissions.add(permission.name)

        denied = denied_permissions(user)
        if denied:
            permissions -= denied | {"playbooks.*"}
        return permissions

    @staticmethod
    def has_permission(user: User, permission_name: str) -> bool:
        """
        Check if a user has a specific permission.
        permission_name format: 'resource.action' (e.g., 'clients.read', 'orders.create')
        """
        # doc 70 §4: the managed-playbooks overlay wins over every grant, `*` included.
        if _overlay_denies(denied_permissions(user), permission_name):
            return False

        user_permissions = PermissionChecker.get_user_permissions(user)

        # Check for wildcard permission (admin)
        if '*' in user_permissions:
            return True

        # Check for exact permission
        if permission_name in user_permissions:
            return True

        # Check for resource-level wildcard (e.g., 'clients.*' grants all client operations)
        resource = permission_name.split('.')[0] if '.' in permission_name else permission_name
        resource_wildcard = f"{resource}.*"
        if resource_wildcard in user_permissions:
            return True

        return False

    @staticmethod
    def has_any_permission(user: User, permission_names: List[str]) -> bool:
        """Check if user has any of the specified permissions"""
        return any(PermissionChecker.has_permission(user, perm) for perm in permission_names)

    @staticmethod
    def get_user_by_id_with_roles(db: Session, user_id: UUID) -> Optional["User"]:
        """
        Fetch a user with their roles and permissions eagerly loaded.
        This is more efficient than lazy loading for permission checks.
        """
        from sqlalchemy.orm import joinedload
        from database_utils.models.auth import User, Role

        user = db.query(User).options(
            joinedload(User.roles).joinedload(Role.permissions),
            joinedload(User.company),  # the doc 70 overlay reads it: no extra query
        ).filter(User.id == user_id).first()

        return user


def require_permission(permission_name: str, get_db_func):
    """
    Dependency factory for FastAPI endpoints to require specific permissions.
    Returns a dependency callable that validates permissions and returns the user.

    Args:
        permission_name: The required permission (e.g., "clients.read")
        get_db_func: The database session dependency function

    Usage:
        from dependencies.db import get_db

        @router.get("/clients")
        async def get_clients(
            user: User = Depends(require_permission("clients.read", get_db)),
            db: Session = Depends(get_db)
        ):
            ...
    """
    async def permission_dependency(
        request: Request,
        db: Session = Depends(get_db_func)
    ) -> User:
        from database_utils.utils.jwt_utils import decode_token, is_refresh_payload
        from database_utils.utils.sessions import check_session

        # Extract token from cookie
        token = get_token_from_header(request)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # Decode token
        payload = decode_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
        # A refresh token is not a bearer credential for the API.
        if is_refresh_payload(payload):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type"
            )
        # Revoked session (logout / reuse / deactivation / password change).
        check_session(db, payload)

        user_id = payload.get("id")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )

        # The JWT carries the user id as a string, but User.id is a SQLAlchemy
        # Uuid column whose bind processor expects a uuid.UUID. Postgres tolerates
        # the string via the driver; SQLite does not (raises "'str' object has no
        # attribute 'hex'"). Coerce explicitly so the query works on both dialects.
        if isinstance(user_id, str):
            try:
                user_id = UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token payload"
                )

        # Get user with permissions
        user = PermissionChecker.get_user_by_id_with_roles(db, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )

        # Same rule as get_current_user: a deactivated account keeps no
        # access even while its token is still valid.
        if not user.active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deactivated"
            )

        # doc 70 §4: an overlay denial carries its own code so the UI and MCP
        # can explain it.
        if _overlay_denies(denied_permissions(user), permission_name):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLAYBOOKS_MANAGED_DETAIL)

        # Check permission
        if not PermissionChecker.has_permission(user, permission_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required permission: {permission_name}"
            )

        return user

    return permission_dependency


# Common permission constants
class Permissions:
    """Centralized permission name constants"""

    # Payment ledger permissions (doc 16 §1). Grants are copied by migration
    # c1b_backfill from orders.update / orders.read / orders.revert_payment.
    PAYMENTS_RECORD = "payments.record"
    PAYMENTS_READ = "payments.read"
    PAYMENTS_REFUND = "payments.refund"

    # Dashboard permissions
    DASHBOARD_READ = "dashboard.read"
