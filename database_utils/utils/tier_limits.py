"""
Tier-based usage limit enforcement.

Call check_tier_limit() at the top of any POST (create) endpoint to block
the operation when the company has reached their plan's limit.
"""
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session


RESOURCE_LIMIT_KEYS: dict[str, str] = {
    "clients": "max_clients",
    "users": "max_users",
}


def check_tier_limit(db: Session, company_id: UUID, resource: str) -> None:
    """
    Raises HTTP 402 if the company has reached the tier limit for the resource.

    Args:
        db: SQLAlchemy database session
        company_id: UUID of the company to check
        resource: One of "clients", "users"

    Raises:
        HTTPException(402): When the current count >= the tier's configured limit
    """
    # Inline imports to avoid circular import issues at module load time
    from database_utils.models.auth import Subscription

    limit_key = RESOURCE_LIMIT_KEYS.get(resource)
    if not limit_key:
        return  # Unknown resource — no limit enforced

    subscription = (
        db.query(Subscription)
        .filter(Subscription.company_id == company_id)
        .first()
    )

    if not subscription or not subscription.tier:
        return  # No subscription found — no limit enforced

    features: dict = subscription.tier.features or {}
    limit = features.get(limit_key)

    # None or -1 means unlimited
    if limit is None or limit == -1:
        return

    # Count existing records for this company
    current_count = _count_resource(db, company_id, resource)

    if current_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Plan limit reached: your plan allows {limit} {resource}. "
                "Upgrade your plan to add more."
            ),
        )


def _count_resource(db: Session, company_id: UUID, resource: str) -> int:
    """Count existing records of the given resource type for a company."""
    if resource == "clients":
        from database_utils.models.crm import Client
        return db.query(Client).filter(Client.company_id == company_id).count()
    elif resource == "users":
        from database_utils.models.auth import User
        return db.query(User).filter(
            User.company_id == company_id,
            User.active == True,  # noqa: E712
        ).count()
    return 0
