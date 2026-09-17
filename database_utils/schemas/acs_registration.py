# schemas/acs_registration.py
"""
ACS device registration (Cycle 5 Phase 1, canon C13): serial/OUI -> tenant
mapping. Plan: docs/isp-platform/23-network-config-implementation-plan.md §2.3.

The (oui, serial_number) identity is GLOBAL-unique; cross-tenant duplicate
pre-registration is a 409 in the router. Status is DERIVED (no enum) and
exposed here as a computed `state` field (PRE_REGISTERED/ONLINE/STALE/
QUARANTINED). Per-device CWMP connection-request secrets are NEVER serialized.
"""
import re
from pydantic import BaseModel, ConfigDict, computed_field, field_validator
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import ACS_STALE_AFTER_SECONDS
from database_utils.utils.timezone_utils import now_gt, make_aware_gt

_OUI_PATTERN = r'^[0-9A-F]{6}$'


def _normalize_serial(v: str) -> str:
    return v.strip().upper()


def _normalize_oui(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return v
    o = v.strip().upper()
    if not re.match(_OUI_PATTERN, o):
        raise ValueError(f"oui must match {_OUI_PATTERN} (6 hex chars) or be empty")
    return o


class AcsRegistrationBase(BaseModel):
    serial_number: str
    oui: Optional[str] = None
    inventory_item_id: Optional[UUID] = None

    @field_validator("serial_number")
    @classmethod
    def validate_serial(cls, v: str) -> str:
        return _normalize_serial(v)

    @field_validator("oui")
    @classmethod
    def validate_oui(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_oui(v)


class AcsRegistrationCreate(AcsRegistrationBase):
    pass


class AcsRegistrationBulkCreate(BaseModel):
    """CSV-import payload (canon C20 production backfill B): per-row conflict
    reporting against the global (oui, serial_number) unique is the router's
    job — 409s are listed, not fatal."""
    registrations: List[AcsRegistrationCreate]


class AcsRegistrationUpdate(BaseModel):
    oui: Optional[str] = None
    inventory_item_id: Optional[UUID] = None

    @field_validator("oui")
    @classmethod
    def validate_oui(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_oui(v)


class AcsRegistrationOut(AcsRegistrationBase):
    id: UUID
    company_id: Optional[UUID] = None   # NULL = quarantined
    first_inform_at: Optional[datetime] = None
    last_inform_at: Optional[datetime] = None
    genieacs_device_id: Optional[str] = None
    cwmp_cr_username: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def state(self) -> str:
        """Derived status (canon C13); mirrors AcsDeviceRegistration.state so
        both ORM reads and plain dicts resolve identically. Order: company
        NULL -> QUARANTINED first."""
        if self.company_id is None:
            return "QUARANTINED"
        if self.first_inform_at is None:
            return "PRE_REGISTERED"
        if self.last_inform_at is None:
            return "STALE"
        age = (now_gt() - make_aware_gt(self.last_inform_at)).total_seconds()
        return "ONLINE" if age <= ACS_STALE_AFTER_SECONDS else "STALE"
