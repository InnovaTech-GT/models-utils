# schemas/device_action_log.py
"""
Device action log (Cycle 5 Phase 1, canon C14): append-only device audit trail.
Plan: docs/isp-platform/23-network-config-implementation-plan.md §2.7.

Read-only surface only — NO Create/Update/Delete schemas exist: rows are
worker/backend-written and immutable (append-only enforced by a Postgres
BEFORE UPDATE/DELETE trigger, revision nc1b).
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, Any
from uuid import UUID
from datetime import datetime


class DeviceActionLogOut(BaseModel):
    id: UUID
    company_id: UUID
    actor_user_id: Optional[UUID] = None
    actor_kind: str
    device_kind: Optional[str] = None
    device_identity: Optional[str] = None
    action: str
    before_data: Optional[Any] = None
    after_data: Optional[Any] = None
    provisioning_job_id: Optional[UUID] = None
    detail: Optional[Any] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
