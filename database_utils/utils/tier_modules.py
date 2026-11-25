"""Tier-module entitlement, one home (security audit #77 support).

Moved from auth-erp `routers/auth.py` (`/me/permissions`) so backend-erp's
`require_module` and auth-erp read the same rule: the company's subscription in
ACTIVE / TRIALING / PAST_DUE supplies `tier.modules`; otherwise (or with a tier
that lists none) the company gets `["core", "admin"]`. PAST_DUE keeps module
access (billing grace): modules disappear only on explicit
suspension/cancellation.
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from database_utils.models.auth import Subscription

DEFAULT_MODULES = ("core", "admin")
ENTITLED_STATUSES = ("ACTIVE", "TRIALING", "PAST_DUE")


def company_modules(db: Session, company_id: Optional[UUID]) -> List[str]:
    if company_id is None:
        return list(DEFAULT_MODULES)
    subscription = (
        db.query(Subscription)
        .options(joinedload(Subscription.tier))
        .filter(
            Subscription.company_id == company_id,
            Subscription.status.in_(ENTITLED_STATUSES),
        )
        .first()
    )
    if subscription and subscription.tier and subscription.tier.modules:
        return list(subscription.tier.modules)
    return list(DEFAULT_MODULES)
