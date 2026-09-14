# schemas/provisioning_settings.py
"""
Provisioning settings (Cycle 5 Phase 1, canon C6): the tenant provisioning
enable gate — a singleton per tenant. Plan:
docs/isp-platform/23-network-config-implementation-plan.md §2.6.

Singleton get-or-create semantics: no Create/Delete schema — the router lazily
creates the row (disabled = fail-safe) on first read and PATCHes it via
`Update`.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional
from uuid import UUID
from datetime import datetime


class ProvisioningSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    default_inform_interval: Optional[int] = None


class ProvisioningSettingsOut(BaseModel):
    id: UUID
    company_id: UUID
    enabled: bool
    default_inform_interval: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
