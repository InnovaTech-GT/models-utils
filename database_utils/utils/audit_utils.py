# utils/audit_utils.py
from typing import Optional, Any, Dict, Union
from uuid import UUID
from sqlalchemy.orm import Session
from database_utils.models.auth import AuditLog
from database_utils.utils.json_utils import serialize_for_json
from loguru import logger
from pydantic import BaseModel


def create_audit_log(
    db: Session,
    user_id: Optional[UUID],
    action: str,
    resource_type: str,
    resource_id: Optional[Union[UUID, int]] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None
) -> AuditLog:
    """
    Create an audit log entry for tracking user actions.

    Args:
        db: Database session
        user_id: ID of the user performing the action (None for unauthenticated actions)
        action: Action being performed (e.g., "client.create", "user.update", "company.disable")
        resource_type: Type of resource (e.g., "client", "user", "company", "tier")
        resource_id: ID of the affected resource
        details: Additional context (before/after state, created data, etc.)
        ip_address: IP address of the user

    Returns:
        Created AuditLog instance

    Example:
        # For CREATE operations
        create_audit_log(
            db=db,
            user_id=user.id,
            action="client.create",
            resource_type="client",
            resource_id=new_client.id,
            details={"data": client_data},
            ip_address="192.168.1.1"
        )

        # For UPDATE operations
        create_audit_log(
            db=db,
            user_id=user.id,
            action="user.update",
            resource_type="user",
            resource_id=user.id,
            details={
                "before": {"name": "Old Name", "email": "old@email.com"},
                "after": {"name": "New Name", "email": "new@email.com"}
            },
            ip_address="192.168.1.1"
        )

        # For DELETE operations
        create_audit_log(
            db=db,
            user_id=user.id,
            action="product.delete",
            resource_type="product",
            resource_id=product_id,
            details={"deleted_data": {"name": product.name, "price": product.price}},
            ip_address="192.168.1.1"
        )
    """
    # Serialize details to ensure all nested objects (Pydantic models, datetime, etc.)
    # are JSON-serializable before storing in the database
    serialized_details = serialize_for_json(details) if details else None

    audit_log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=serialized_details,
        ip_address=ip_address
    )

    db.add(audit_log)
    db.commit()
    db.refresh(audit_log)

    logger.info(
        f"Audit log created: user_id={user_id}, action={action}, "
        f"resource_type={resource_type}, resource_id={resource_id}"
    )

    return audit_log


def log_create_operation(
    db: Session,
    user_id: Optional[UUID],
    resource_type: str,
    resource_id: Union[UUID, int],
    resource_data: Any,
    ip_address: Optional[str] = None
) -> AuditLog:
    """
    Convenience function to log CREATE operations.

    Args:
        db: Database session
        user_id: ID of the user performing the action
        resource_type: Type of resource created (e.g., "client", "user")
        resource_id: ID of the created resource
        resource_data: The data of the created resource (dict or Pydantic model)
        ip_address: IP address of the user

    Returns:
        Created AuditLog instance
    """
    # Convert Pydantic model to dict if necessary
    if isinstance(resource_data, BaseModel):
        data = resource_data.dict()
    elif isinstance(resource_data, dict):
        data = resource_data
    else:
        # Try to convert SQLAlchemy model to dict
        data = {c.name: getattr(resource_data, c.name) for c in resource_data.__table__.columns}

    return create_audit_log(
        db=db,
        user_id=user_id,
        action=f"{resource_type}.create",
        resource_type=resource_type,
        resource_id=resource_id,
        details={"created_data": data},
        ip_address=ip_address
    )


def log_update_operation(
    db: Session,
    user_id: Optional[UUID],
    resource_type: str,
    resource_id: Union[UUID, int],
    before_data: Any,
    after_data: Any,
    ip_address: Optional[str] = None
) -> AuditLog:
    """
    Convenience function to log UPDATE operations.

    Args:
        db: Database session
        user_id: ID of the user performing the action
        resource_type: Type of resource updated (e.g., "client", "user")
        resource_id: ID of the updated resource
        before_data: State before update (dict, Pydantic model, or SQLAlchemy model)
        after_data: State after update (dict, Pydantic model, or SQLAlchemy model)
        ip_address: IP address of the user

    Returns:
        Created AuditLog instance
    """
    def to_dict(data: Any) -> dict:
        if isinstance(data, BaseModel):
            return data.dict()
        elif isinstance(data, dict):
            return data
        else:
            # SQLAlchemy model
            return {c.name: getattr(data, c.name) for c in data.__table__.columns}

    return create_audit_log(
        db=db,
        user_id=user_id,
        action=f"{resource_type}.update",
        resource_type=resource_type,
        resource_id=resource_id,
        details={
            "before": to_dict(before_data),
            "after": to_dict(after_data)
        },
        ip_address=ip_address
    )


def log_delete_operation(
    db: Session,
    user_id: Optional[UUID],
    resource_type: str,
    resource_id: Union[UUID, int],
    resource_data: Any,
    ip_address: Optional[str] = None
) -> AuditLog:
    """
    Convenience function to log DELETE operations.

    Args:
        db: Database session
        user_id: ID of the user performing the action
        resource_type: Type of resource deleted (e.g., "client", "user")
        resource_id: ID of the deleted resource
        resource_data: The data of the resource before deletion
        ip_address: IP address of the user

    Returns:
        Created AuditLog instance
    """
    # Convert to dict
    if isinstance(resource_data, BaseModel):
        data = resource_data.dict()
    elif isinstance(resource_data, dict):
        data = resource_data
    else:
        # SQLAlchemy model
        data = {c.name: getattr(resource_data, c.name) for c in resource_data.__table__.columns}

    return create_audit_log(
        db=db,
        user_id=user_id,
        action=f"{resource_type}.delete",
        resource_type=resource_type,
        resource_id=resource_id,
        details={"deleted_data": data},
        ip_address=ip_address
    )


def log_custom_operation(
    db: Session,
    user_id: Optional[UUID],
    action: str,
    resource_type: str,
    resource_id: Optional[Union[UUID, int]] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None
) -> AuditLog:
    """
    Convenience function to log custom operations (not CRUD).

    Args:
        db: Database session
        user_id: ID of the user performing the action
        action: Custom action name (e.g., "user.activate", "subscription.cancel")
        resource_type: Type of resource
        resource_id: ID of the affected resource
        details: Additional context
        ip_address: IP address of the user

    Returns:
        Created AuditLog instance
    """
    return create_audit_log(
        db=db,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address
    )


def serialize_for_audit(data: dict) -> dict:
    """Convert a model column dict to JSON-safe types for audit logging.

    Handles UUID, datetime, date, and Decimal types that are not
    JSON-serializable by default.

    Args:
        data: Dictionary of model column values to serialize.

    Returns:
        A new dictionary with all non-JSON-serializable values converted to str.
    """
    import enum
    from uuid import UUID
    from datetime import datetime, date
    from decimal import Decimal

    def _convert(v):
        # Enums first: str(StrEnum) is "ClassName.MEMBER", which breaks
        # workflow field-condition matching (e.g. changed_to "SUCCEEDED").
        if isinstance(v, enum.Enum):
            return _convert(v.value)
        if isinstance(v, (UUID, datetime, date, Decimal)):
            return str(v)
        if isinstance(v, dict):
            return {k: _convert(val) for k, val in v.items()}
        if isinstance(v, list):
            return [_convert(item) for item in v]
        return v

    return {k: _convert(v) for k, v in data.items()}
