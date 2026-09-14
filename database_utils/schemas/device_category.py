# schemas/device_category.py
"""
Device category (Cycle 3 E4, doc 20a admin-categories-sidebar): a platform-
global, super-admin-managed table (auth-erp admin_device_categories.py)
replacing the `devicecategory` PG enum (dropped in revision
c3b_device_categories). No company_id — SaaS staff own the list; tenants read
it via a backend-erp read-only endpoint.

`key` is immutable after creation (omitted from Update — repointing it would
silently detach every device_type/playbook FK backfilled against it) and
`is_system` is never client-settable (the 13 baseline rows are seed-owned;
deletion of an is_system row is blocked at the router, not here).
"""
import re
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import DEVICE_CATEGORY_TIERS

# Key format per appendix admin-categories-sidebar §2: uppercase snake,
# 2-51 chars, first char a letter (mirrors the topology purpose pattern's
# shape but is a distinct constant — categories and purposes are unrelated
# namespaces).
_KEY_PATTERN = r'^[A-Z][A-Z0-9_]{1,50}$'


def _normalize_tier(v: Optional[str]) -> Optional[str]:
    """Cycle 7 (doc 25 §2.1): tier is a CHECK-constrained string on the model
    (DEVICE_CATEGORY_TIERS) — normalize + validate here so the router never
    hands the DB a value the CHECK would reject with a raw 500."""
    if v is None:
        return v
    t = v.strip().upper()
    if not t:
        return None
    if t not in DEVICE_CATEGORY_TIERS:
        raise ValueError(f"tier must be one of {list(DEVICE_CATEGORY_TIERS)} (got '{v}')")
    return t


class DeviceCategoryBase(BaseModel):
    name: str
    sort_order: int = 0
    icon: Optional[str] = None
    is_active: bool = True
    # Cycle 7 (doc 25 §2.1): CORE/EDGE axis; NULL = passives/unclassified.
    # SaaS-admin editable like name/icon (key stays immutable).
    tier: Optional[str] = None

    @field_validator('tier')
    @classmethod
    def validate_tier(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_tier(v)


class DeviceCategoryCreate(DeviceCategoryBase):
    key: str

    @field_validator('key')
    @classmethod
    def validate_key(cls, v: str) -> str:
        k = v.strip().upper()
        if not re.match(_KEY_PATTERN, k):
            raise ValueError(f"key must match {_KEY_PATTERN} (got '{v}')")
        return k


class DeviceCategoryUpdate(BaseModel):
    """key and is_system are immutable on update — a super-admin renames the
    display name/sort_order/icon or deactivates a category, but never
    repoints the stable key every device_type/playbook FK was backfilled
    against (revision c3b_device_categories)."""
    name: Optional[str] = None
    sort_order: Optional[int] = None
    icon: Optional[str] = None
    is_active: Optional[bool] = None
    # Cycle 7 (doc 25 §2.1): tier IS editable (unlike key) — a super-admin may
    # classify custom categories or clear a tier back to NULL (passives).
    tier: Optional[str] = None

    @field_validator('tier')
    @classmethod
    def validate_tier(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_tier(v)


class DeviceCategoryOut(DeviceCategoryBase):
    id: UUID
    key: str
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
