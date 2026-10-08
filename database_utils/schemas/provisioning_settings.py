# schemas/provisioning_settings.py
"""
Provisioning settings (canon C6 + C9): the tenant's provisioning enable gate,
transport axis and ACS configuration — one singleton row per tenant. Plan:
docs/isp-platform/23-network-config-implementation-plan.md §2.6, extended by
revision `tr1_transport_axis`, which folded the whole multi-row `network_access`
table (and its `schemas/network_access.py`, deleted) in here.

Singleton get-or-create semantics: no Create/Delete schema — the router lazily
creates the row (disabled = fail-safe) on first read and PATCHes it via `Update`.

`acs_base_url` is on `Out` and deliberately NOT on `Update`. Nothing in code
reads it; it exists to tell an installer what to type into a CPE, and the router
applies `Update` with a blanket setattr loop, so keeping the field off the write
schema is what makes "read-only" structural instead of a guard someone can
forget. An `http://` value there would turn every CWMP POST into a bodyless GET
at Railway's edge, and a CPE pointed at a dead URL has no remote fix.
"""
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing import Optional
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import DIAL_TARGETS, PROXY_KINDS


# Update fields whose provisioning_settings column is NOT NULL.
_NOT_NULL_FIELDS = frozenset({"enabled", "dial_target", "proxy_kind", "acs_auth_required", "ztp_enabled"})


class ProvisioningSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    default_inform_interval: Optional[int] = None
    # --- the transport axis (tr1_transport_axis) ---------------------------
    dial_target: Optional[str] = None
    proxy_kind: Optional[str] = None
    proxy_address: Optional[str] = None
    gateway_host: Optional[str] = None
    # --- Capa 3 (ac1/decision 8) -------------------------------------------
    # Arming this is gated in backend-erp's router BEFORE the setattr loop
    # (CWMP_CREDENTIAL_MISSING / CWMP_ROLLOUT_INCOMPLETE / ROLLOUT_UNVERIFIABLE,
    # overridable with ?force=true): arming a tenant whose CPEs have not all
    # received the credential locks those CPEs out, and the ACS cannot fix it
    # because fixing it requires a session.
    acs_auth_required: Optional[bool] = None
    # --- ZTP (zt1, doc 43 §4) ------------------------------------------------
    ztp_enabled: Optional[bool] = None

    @model_validator(mode="after")
    def reject_null_on_not_null_columns(self):
        """The router applies this with a blind setattr loop over
        model_dump(exclude_unset=True), so an explicit null on a NOT NULL column
        was a 500 IntegrityError. Omitted fields stay omitted."""
        for name in sorted(self.model_fields_set & _NOT_NULL_FIELDS):
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    @field_validator("dial_target")
    @classmethod
    def validate_dial_target(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in DIAL_TARGETS:
            raise ValueError(f"dial_target must be one of {sorted(DIAL_TARGETS)}")
        return v

    @field_validator("proxy_kind")
    @classmethod
    def validate_proxy_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in PROXY_KINDS:
            raise ValueError(f"proxy_kind must be one of {sorted(PROXY_KINDS)}")
        return v

    @model_validator(mode="after")
    def validate_axis_operands(self):
        """Mirrors ck_provisioning_settings_proxy_address / _gateway_host so the
        API answers 422 instead of letting the DB CHECK surface as a 500 — but
        only for what IS visible here.

        This schema sees the fields present in THIS payload, never the row's
        current state, so it can only catch "switch the axis AND blank its
        operand in one request". The two cases it cannot close are the router's,
        which is the only layer that sees the merged row, and which answers
        GATEWAY_HOST_REQUIRED / PROXY_ADDRESS_REQUIRED:

          * `{"proxy_kind": "socks5"}` alone — legal when the row already holds a
            proxy_address, an IntegrityError when it does not. Demanding the
            operand here would forbid the legal case.
          * `{"proxy_address": ""}` on a row that is already socks5 — `''` is NOT
            NULL, so the CHECK passes and the row commits with a blank hop. The
            resolver then fails closed with PROXY_NOT_PROVISIONED rather than
            dialling unproxied, which is why this is a config bug and not a
            security hole.
        """
        if self.proxy_kind == "socks5" and self.proxy_address is not None:
            if not self.proxy_address.strip():
                raise ValueError("proxy_address is required when proxy_kind is 'socks5'")
        if self.dial_target == "gateway" and self.gateway_host is not None:
            if not self.gateway_host.strip():
                raise ValueError("gateway_host is required when dial_target is 'gateway'")
        return self


class ProvisioningSettingsOut(BaseModel):
    id: UUID
    company_id: UUID
    enabled: bool
    default_inform_interval: Optional[int] = None
    dial_target: str
    proxy_kind: str
    proxy_address: Optional[str] = None
    gateway_host: Optional[str] = None
    # Read-only (see the module docstring).
    acs_base_url: Optional[str] = None
    acs_auth_required: bool = False
    ztp_enabled: bool = False
    # The tenant's TR-069 Inform credential and, during a rotation window, its
    # successor. Ids only — the secret never round-trips (canon C19); the
    # credential's own fingerprint/has_secret come from DeviceCredentialOut.
    cwmp_credential_id: Optional[UUID] = None
    cwmp_pending_credential_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
