"""company_modules: the one home for tier-module entitlement (#77 support),
moved from auth-erp /me/permissions."""
import uuid
from datetime import timedelta

import pytest

from database_utils.models.auth import Subscription, Tier
from database_utils.utils.tier_modules import company_modules
from database_utils.utils.timezone_utils import now_gt

MODS = ["core", "admin", "management", "automations"]


def _sub(db, status, modules=MODS):
    tier = Tier(name=f"t-{uuid.uuid4().hex[:6]}", modules=modules)
    db.add(tier)
    db.flush()
    co = uuid.uuid4()
    db.add(Subscription(company_id=co, tier_id=tier.id, status=status,
                        current_period_start=now_gt(), current_period_end=now_gt() + timedelta(days=30)))
    db.flush()
    return co


@pytest.mark.parametrize("status", ["ACTIVE", "TRIALING"])
def test_active_subscription_modules(db, status):
    assert company_modules(db, _sub(db, status)) == MODS


def test_past_due_keeps_modules(db):
    assert company_modules(db, _sub(db, "PAST_DUE")) == MODS


def test_cancelled_falls_back_to_core_admin(db):
    assert company_modules(db, _sub(db, "CANCELED")) == ["core", "admin"]


def test_no_subscription_falls_back(db):
    assert company_modules(db, uuid.uuid4()) == ["core", "admin"]
    assert company_modules(db, None) == ["core", "admin"]


def test_tier_without_modules_falls_back(db):
    assert company_modules(db, _sub(db, "ACTIVE", modules=None)) == ["core", "admin"]


def test_result_is_a_copy(db):
    co = _sub(db, "ACTIVE")
    company_modules(db, co).append("x")
    assert company_modules(db, co) == MODS
