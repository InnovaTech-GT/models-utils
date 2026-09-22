# schemas/network_access.py
"""
Network access (Cycle 5 Phase 1, canon C9): per-tenant transport configuration,
multiple rows per tenant keyed by `kind` (acs|olt). Plan:
docs/isp-platform/23-network-config-implementation-plan.md §2.2.

CIDR validation (valid networks) lives here in the schema, not the DB — same
tier as the topology-purpose normalizer. Standard multi-record CRUD (no
singleton PUT).
"""
import ipaddress
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import NETWORK_ACCESS_KINDS, NETWORK_ACCESS_MODES, NAT_MODES


def _validate_subnets(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return v
    for cidr in v:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid CIDR '{cidr}': {exc}") from exc
    return v


class NetworkAccessBase(BaseModel):
    name: str
    kind: str
    mode: str = "direct"
    is_default: bool = False
    mgmt_subnets: Optional[List[str]] = None
    acs_base_url: Optional[str] = None
    # spec §8: deliberately a bare string. Under nat_zt this is an RFC1918
    # ZeroTier address; under nat_public a public IP or a DDNS hostname. No
    # ip_address() coercion, no routability assertion.
    gateway_host: Optional[str] = None
    # spec 2026-08-17 N13: the tenant's own Pylon SOCKS5 listener,
    # "host:port". Required only for nat_zt — nat_public has no proxy hop.
    pylon_socks5: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        if v not in NETWORK_ACCESS_KINDS:
            raise ValueError(f"kind must be one of {sorted(NETWORK_ACCESS_KINDS)}")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in NETWORK_ACCESS_MODES:
            raise ValueError(f"mode must be one of {sorted(NETWORK_ACCESS_MODES)}")
        return v

    @field_validator("mgmt_subnets")
    @classmethod
    def validate_subnets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_subnets(v)

    @model_validator(mode="after")
    def validate_nat_requires_gateway_host(self):
        if self.mode in NAT_MODES and not (self.gateway_host or "").strip():
            raise ValueError(f"gateway_host is required when mode is '{self.mode}'")
        if self.mode == "nat_zt" and not (self.pylon_socks5 or "").strip():
            raise ValueError("pylon_socks5 is required when mode is 'nat_zt'")
        return self


class NetworkAccessCreate(NetworkAccessBase):
    pass


class NetworkAccessUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    mode: Optional[str] = None
    is_default: Optional[bool] = None
    mgmt_subnets: Optional[List[str]] = None
    acs_base_url: Optional[str] = None
    gateway_host: Optional[str] = None
    pylon_socks5: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in NETWORK_ACCESS_KINDS:
            raise ValueError(f"kind must be one of {sorted(NETWORK_ACCESS_KINDS)}")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in NETWORK_ACCESS_MODES:
            raise ValueError(f"mode must be one of {sorted(NETWORK_ACCESS_MODES)}")
        return v

    @field_validator("mgmt_subnets")
    @classmethod
    def validate_subnets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_subnets(v)

    @model_validator(mode="after")
    def validate_nat_requires_gateway_host(self):
        # Whole-branch review I3: belt-and-suspenders with the DB CHECK
        # (nat2_gateway_host_check) and backend-erp's router-level fix. This
        # schema only sees the fields present in THIS update payload, not the
        # row's current DB state, so it can only catch the case where both
        # `mode` (being set to a NAT mode) and a blank `gateway_host` are
        # submitted together in the same request — the exact "direct ->
        # nat_public with no gateway_host" tenant flow the review flagged.
        # The DB CHECK is what closes every other path.
        if self.mode is not None and self.mode in NAT_MODES:
            if self.gateway_host is not None and not self.gateway_host.strip():
                raise ValueError(f"gateway_host is required when mode is '{self.mode}'")
        if self.mode is not None and self.mode == "nat_zt" and self.pylon_socks5 is not None and not self.pylon_socks5.strip():
            raise ValueError("pylon_socks5 is required when mode is 'nat_zt'")
        return self


class NetworkAccessOut(NetworkAccessBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
