# schemas/device_credential.py
"""
Device credential (Cycle 5 Phase 1, canon C1/C19): envelope-encrypted per-tenant
device secrets. Plan: docs/isp-platform/23-network-config-implementation-plan.md
§2.1 + §2.12.

Secrets never round-trip (canon C19): `Create`/`Update` accept a plaintext
`secret` string, the router encrypts it via database_utils.utils.crypto before
insert, and `Out` NEVER carries the ciphertext/dek/plaintext — only
`has_secret` + `fingerprint` (last 4). Binding FKs live ON the credential row
(inventory_item > device_type > unbound-company-default resolution order —
`tr1_transport_axis` removed the `network_access_id` tier with the table it
pointed at, so a credential with both FKs NULL IS the company default); the
router verifies each binding is same-company.
"""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Optional
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import CREDENTIAL_KINDS


def _validate_kind(v: str) -> str:
    if v not in CREDENTIAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(CREDENTIAL_KINDS)}")
    return v


def require_cpe_web_binding(kind, inventory_item_id, device_type_id) -> None:
    """Doc 77 D4: a CPE_WEB credential is never a company default — it must be
    bound to an item or a device type. Create checks it here; Update is
    partial, so the router must call this on the MERGED row."""
    if kind == "CPE_WEB" and inventory_item_id is None and device_type_id is None:
        raise ValueError("CPE_WEB_BINDING_REQUIRED: a CPE_WEB credential needs "
                         "inventory_item_id or device_type_id (never a company default)")


class DeviceCredentialBase(BaseModel):
    name: str
    kind: str
    username: Optional[str] = None
    # Bindings (canon C19) — both optional; resolution order is
    # inventory_item > device_type > unbound (both NULL = the company default).
    inventory_item_id: Optional[UUID] = None
    device_type_id: Optional[UUID] = None
    # ow1 (doc 77 D4): try order among matching credentials (lower first).
    priority: int = Field(0, ge=0, le=1000)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        return _validate_kind(v)


class DeviceCredentialCreate(DeviceCredentialBase):
    # Plaintext secret — encrypted by the router via crypto.py before insert
    # and never persisted in the clear. Required on create.
    secret: str

    @model_validator(mode="after")
    def cpe_web_needs_a_binding(self):
        require_cpe_web_binding(self.kind, self.inventory_item_id, self.device_type_id)
        return self


class DeviceCredentialUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    username: Optional[str] = None
    inventory_item_id: Optional[UUID] = None
    device_type_id: Optional[UUID] = None
    # Omitted = keep; an explicit null is refused (NOT NULL column).
    priority: Optional[int] = Field(None, ge=0, le=1000)
    # None = keep the existing secret; a value rotates it (router re-encrypts
    # and stamps last_rotated_at).
    secret: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_kind(v)


    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: Optional[int]) -> int:
        if v is None:
            raise ValueError("priority cannot be null")
        return v


class CredentialRotateRequest(BaseModel):
    """Dedicated rotate flow (canon C20 secret-rotation runbook): replace the
    secret and stamp last_rotated_at without touching bindings/metadata."""
    secret: str


class DeviceCredentialOut(DeviceCredentialBase):
    id: UUID
    company_id: UUID
    # Secrets are write-only (canon C19): expose only presence + fingerprint.
    has_secret: bool
    fingerprint: Optional[str] = None
    kek_id: str
    last_rotated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
