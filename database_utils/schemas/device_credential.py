# schemas/device_credential.py
"""
Device credential (Cycle 5 Phase 1, canon C1/C19): envelope-encrypted per-tenant
device secrets. Plan: docs/isp-platform/23-network-config-implementation-plan.md
§2.1 + §2.12.

Secrets never round-trip (canon C19): `Create`/`Update` accept a plaintext
`secret` string, the router encrypts it via database_utils.utils.crypto before
insert, and `Out` NEVER carries the ciphertext/dek/plaintext — only
`has_secret` + `fingerprint` (last 4). Binding FKs live ON the credential row
(inventory_item > device_type > network_access resolution order); the router
verifies each binding is same-company.
"""
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import CREDENTIAL_KINDS


def _validate_kind(v: str) -> str:
    if v not in CREDENTIAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(CREDENTIAL_KINDS)}")
    return v


class DeviceCredentialBase(BaseModel):
    name: str
    kind: str
    username: Optional[str] = None
    # Bindings (canon C19) — all optional; resolution order is
    # inventory_item > device_type > network_access.
    inventory_item_id: Optional[UUID] = None
    device_type_id: Optional[UUID] = None
    network_access_id: Optional[UUID] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        return _validate_kind(v)


class DeviceCredentialCreate(DeviceCredentialBase):
    # Plaintext secret — encrypted by the router via crypto.py before insert
    # and never persisted in the clear. Required on create.
    secret: str


class DeviceCredentialUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    username: Optional[str] = None
    inventory_item_id: Optional[UUID] = None
    device_type_id: Optional[UUID] = None
    network_access_id: Optional[UUID] = None
    # None = keep the existing secret; a value rotates it (router re-encrypts
    # and stamps last_rotated_at).
    secret: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_kind(v)


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
