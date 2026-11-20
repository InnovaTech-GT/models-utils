"""dm1 (doc 75): tenant-defined SNMP device metrics, API shapes.

OIDs are numeric only (MIB names are never resolved: neither the backend nor
the poller ships MIBs). A leading dot is stripped. Exactly one scope is set:
``device_type_id`` (every device of the type) or ``inventory_item_id``.
"""
import math
import re
from datetime import datetime
from typing import Any, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database_utils.models.device_metric import DEVICE_METRIC_MAX_INSTANCES

_OID = re.compile(r"^1(\.\d{1,10}){3,}$")


def normalize_oid(v: Optional[str]) -> Optional[str]:
    """Strip spaces and one leading dot; raise ``INVALID_OID: ...`` unless numeric dotted."""
    if v is None:
        return None
    v = v.strip().lstrip(".")
    if not v:
        return None
    if len(v) > 200 or not _OID.match(v):
        raise ValueError(f"INVALID_OID: {v[:60]!r} is not a numeric OID like 1.3.6.1.2.1.1.3.0")
    return v


class _MetricFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    unit: Optional[str] = Field(None, max_length=16)
    oid: Optional[str] = None
    kind: Optional[Literal["GAUGE", "COUNTER"]] = None
    label_oid: Optional[str] = None
    instances: Optional[List[str]] = None
    subtract_from_oid: Optional[str] = None
    scale: Optional[float] = None
    enabled: Optional[bool] = None

    @field_validator("name", "unit")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("oid", "label_oid", "subtract_from_oid")
    @classmethod
    def _oid(cls, v):
        return normalize_oid(v)

    @field_validator("instances")
    @classmethod
    def _instances(cls, v):
        if v is None:
            return v
        out = []
        for s in v:
            s = str(s).strip()
            if s and s not in out:
                if len(s) > 128:
                    raise ValueError("an instance name is at most 128 characters")
                out.append(s)
        if len(out) > DEVICE_METRIC_MAX_INSTANCES:
            raise ValueError(f"at most {DEVICE_METRIC_MAX_INSTANCES} instances")
        return out

    @field_validator("scale")
    @classmethod
    def _scale(cls, v):
        if v is not None and (not math.isfinite(v) or v == 0 or abs(v) > 1e9):
            raise ValueError("scale must be a finite, non-zero number up to 1e9")
        return v


class DeviceMetricCreate(_MetricFields):
    name: str = Field(..., min_length=1, max_length=80)
    oid: str
    kind: Literal["GAUGE", "COUNTER"] = "GAUGE"
    instances: List[str] = Field(default_factory=list)
    scale: float = 1.0
    enabled: bool = True
    device_type_id: Optional[UUID] = None
    inventory_item_id: Optional[UUID] = None
    preset: Optional[str] = Field(None, max_length=32)

    @model_validator(mode="after")
    def _one_scope(self):
        if (self.device_type_id is None) == (self.inventory_item_id is None):
            raise ValueError("SCOPE_REQUIRED: set exactly one of device_type_id or inventory_item_id")
        return self


class DeviceMetricUpdate(_MetricFields):
    """Partial: only the keys sent change. The scope is fixed once created."""


class DeviceMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    unit: Optional[str] = None
    oid: str
    kind: str
    label_oid: Optional[str] = None
    instances: List[str] = Field(default_factory=list)
    subtract_from_oid: Optional[str] = None
    scale: float
    device_type_id: Optional[UUID] = None
    inventory_item_id: Optional[UUID] = None
    preset: Optional[str] = None
    enabled: bool
    last_poll: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


__all__ = ["normalize_oid", "DeviceMetricCreate", "DeviceMetricUpdate", "DeviceMetricOut"]
