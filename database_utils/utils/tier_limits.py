"""
Tier-based usage limits, billable-client count and billing access (doc 61).

Call check_tier_limit() at the top of any POST (create) endpoint to block
the operation when the company has reached their plan's limit, and
billing_access() to gate a tenant that has not paid.

A limit's value comes from Subscription.limit_overrides (per-tenant, set by
the superadmin), else the tier's features JSON. None or -1 = unlimited.
"""
import math
from datetime import timedelta
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

RESOURCE_LIMIT_KEYS: dict[str, str] = {
    "clients": "max_clients",
    "users": "max_users",
}

# Doc 61 limit keys (tier.features / subscription.limit_overrides).
CLIENT_CAP_MIN = "client_cap_min"        # non-deactivated clients allowed regardless of billable
CLIENT_CAP_RATIO = "client_cap_ratio"    # ... or this multiple of the billable count
MAX_CONCURRENT_RUNS = "max_concurrent_runs"

# Billing access (doc 61 §7): days past an unpaid invoice's due date.
READ_ONLY_AFTER_DAYS = 15
LOCKED_AFTER_DAYS = 30
ACCESS_FULL, ACCESS_READ_ONLY, ACCESS_LOCKED = "FULL", "READ_ONLY", "LOCKED"


def _subscription(db: Session, company_id: UUID):
    from database_utils.models.auth import Subscription
    return db.query(Subscription).filter(Subscription.company_id == company_id).first()


def effective_limit(subscription, key: str):
    """Per-tenant override, else the tier feature; None = unlimited (-1 too)."""
    if subscription is None:
        return None
    overrides = subscription.limit_overrides or {}
    if key in overrides:
        value = overrides[key]
    else:
        value = ((subscription.tier.features if subscription.tier else None) or {}).get(key)
    return None if value is None or value == -1 else value


def billable_clients(db: Session, company_id: UUID) -> int:
    """Doc 61: clients that are not deactivated and have at least one
    service that is not CANCELLED (active, suspended or pending install)."""
    from sqlalchemy import func
    from database_utils.models.crm import Client
    from database_utils.models.isp import ClientService, ClientServiceStatus
    return (
        db.query(func.count(func.distinct(ClientService.client_id)))
        .join(Client, Client.id == ClientService.client_id)
        .filter(
            ClientService.company_id == company_id,
            ClientService.status != ClientServiceStatus.CANCELLED,
            Client.deactivated_at.is_(None),
        )
        .scalar()
        or 0
    )


def client_cap(subscription, billable: int) -> Optional[int]:
    """Non-deactivated clients allowed: max(min, ceil(ratio × billable))."""
    cap_min = effective_limit(subscription, CLIENT_CAP_MIN)
    ratio = effective_limit(subscription, CLIENT_CAP_RATIO)
    if cap_min is None and ratio is None:
        return None
    # round first: 1.1 * 100 is 110.00000000000001 in floats.
    return max(int(cap_min or 0), math.ceil(round((ratio or 0) * billable, 6)))


def quote_required(subscription, billable: int) -> bool:
    """Doc 61: at quote_above_clients billable clients, growth stops until the
    superadmin sets a quoted price."""
    tier = subscription.tier if subscription else None
    limit = tier.quote_above_clients if tier else None
    return limit is not None and billable >= limit and subscription.custom_monthly_price is None


def check_tier_limit(db: Session, company_id: UUID, resource: str) -> None:
    """
    Raises HTTP 402 if the company has reached the tier limit for the resource.

    Args:
        db: SQLAlchemy database session
        company_id: UUID of the company to check
        resource: "clients" (new client), "services" (new client service) or "users"

    Raises:
        HTTPException(402): When the limit is reached
    """
    subscription = _subscription(db, company_id)
    if not subscription or not subscription.tier:
        return  # No subscription found — no limit enforced

    if resource in ("clients", "services"):
        billable = billable_clients(db, company_id)
        if quote_required(subscription, billable):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"QUOTE_REQUIRED: you have reached {billable} billable clients. "
                    "Contact Uplink for a custom quote to keep growing."
                ),
            )
        if resource == "services":
            return
        cap = client_cap(subscription, billable)
        if cap is not None and _count_resource(db, company_id, "clients") >= cap:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"CLIENT_CAP_REACHED: your plan allows {cap} clients for {billable} billable "
                    "clients. Deactivate clients you no longer serve, or add their services."
                ),
            )

    limit_key = RESOURCE_LIMIT_KEYS.get(resource)
    if not limit_key:
        return  # Unknown resource — no limit enforced
    limit = effective_limit(subscription, limit_key)
    if limit is None:
        return

    # Count existing records for this company
    current_count = _count_resource(db, company_id, resource)

    if current_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Plan limit reached: your plan allows {limit} {resource}. "
                "Contact Uplink to raise it."
            ),
        )


def _count_resource(db: Session, company_id: UUID, resource: str) -> int:
    """Count existing records of the given resource type for a company."""
    if resource == "clients":
        from database_utils.models.crm import Client
        # Doc 61: deactivated clients never count against the cap.
        return db.query(Client).filter(
            Client.company_id == company_id,
            Client.deactivated_at.is_(None),
        ).count()
    elif resource == "users":
        from database_utils.models.auth import User
        return db.query(User).filter(
            User.company_id == company_id,
            User.active == True,
            # pl2: machine accounts ("Bot de WhatsApp") never take a seat.
            User.is_system == False,
        ).count()
    return 0


def billing_access(db: Session, company_id: UUID, now=None) -> str:
    """Doc 61 §7: FULL, READ_ONLY (an unpaid invoice is 15+ days past due) or
    LOCKED (30+ days past due, or no paid-up subscription: INCOMPLETE /
    CANCELED). No subscription = FULL (legacy/internal companies)."""
    from database_utils.models.auth import BillingInvoice
    from database_utils.utils.timezone_utils import make_aware_gt, now_gt

    subscription = _subscription(db, company_id)
    if subscription is None:
        return ACCESS_FULL
    if subscription.status in ("INCOMPLETE", "CANCELED"):
        return ACCESS_LOCKED
    oldest_due = (
        db.query(BillingInvoice.due_date)
        .filter(BillingInvoice.subscription_id == subscription.id, BillingInvoice.status == "PENDING")
        .order_by(BillingInvoice.due_date)
        .limit(1)
        .scalar()
    )
    if oldest_due is None:
        return ACCESS_FULL
    overdue = (now or now_gt()) - make_aware_gt(oldest_due)
    if overdue >= timedelta(days=LOCKED_AFTER_DAYS):
        return ACCESS_LOCKED
    if overdue >= timedelta(days=READ_ONLY_AFTER_DAYS):
        return ACCESS_READ_ONLY
    return ACCESS_FULL
