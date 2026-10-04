# schemas/inventory.py
import re

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator, Field
from typing import Optional, List, Dict, Any, NamedTuple
from uuid import UUID
from datetime import datetime

import sqlalchemy as sa

from database_utils.models.isp import (
    InventoryItemStatus,
    InventoryItemCondition,
    EquipmentEventType,
    CLI_PROTOCOLS,
    DeviceCategory,
    PATH_ROLE_PATTERN,
    PORT_DIRECTIONS,
    PORT_MEDIA,
    PORT_NAME_PATTERN,
)
from database_utils.utils.playbook_expr import is_secret_name


def _normalize_cli_protocol(v: Optional[str]) -> Optional[str]:
    """Cycle 7 (doc 25 §2.3): cli_protocol is a CHECK-constrained string on
    the model (CLI_PROTOCOLS, lowercase driver keys) — normalize + validate
    here so the DB CHECK never fires as a raw 500."""
    if v is None:
        return v
    p = v.strip().lower()
    if not p:
        return None
    if p not in CLI_PROTOCOLS:
        raise ValueError(f"cli_protocol must be one of {list(CLI_PROTOCOLS)} (got '{v}')")
    return p


# --- Attribute schema entries (device_type.attribute_schema) ---

_ALLOWED_ATTR_TYPES = {"TEXT", "NUMBER", "BOOLEAN", "DATE", "ENUM"}


class AttributeDefinition(BaseModel):
    key: str
    label: str
    type: str = "TEXT"
    required: bool = False
    options: Optional[List[str]] = None  # for ENUM
    unit: Optional[str] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in _ALLOWED_ATTR_TYPES:
            raise ValueError(f"type must be one of {sorted(_ALLOWED_ATTR_TYPES)}")
        return v

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError("key must be snake_case (lowercase letters, digits, underscores)")
        return v


# --- Port templates (device_type.port_template, doc 40 §3.1.2) ---

MAX_TEMPLATE_GROUPS = 32
MAX_TEMPLATE_PORTS = 1024
_PORT_NAME = re.compile(PORT_NAME_PATTERN)
_PATH_ROLE = re.compile(PATH_ROLE_PATTERN)


class PortTemplateGroup(BaseModel):
    """One group of generated ports, e.g. 16 PON ports on slot 1:
    {"name": "{slot}/{n}", "slots": [1], "start": 1, "count": 16,
     "medium": "PON", "direction": "DOWN"}.

    Only `{slot}` and `{n}` are placeholders, substituted with str.replace,
    never str.format. Without `slots` the ports get slot NULL."""
    # Bounded BEFORE expansion: expanded names are capped at 32 characters,
    # but that check runs only after every port has been built.
    name: str = Field(max_length=64)
    slots: Optional[List[StrictInt]] = Field(default=None, max_length=256)
    start: StrictInt = 1
    count: StrictInt
    medium: str
    direction: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        rest = v.replace("{slot}", "").replace("{n}", "")
        if "{" in rest or "}" in rest:
            raise ValueError("port name pattern allows only the {slot} and {n} placeholders")
        return v

    @field_validator("slots")
    @classmethod
    def validate_slots(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is None:
            return v
        if not v:
            raise ValueError("slots must not be empty (omit it for slot-less ports)")
        if len(set(v)) != len(v):
            raise ValueError("slots must be unique")
        if any(not 0 <= s <= 255 for s in v):
            raise ValueError("slots must be between 0 and 255")
        return v

    @field_validator("medium")
    @classmethod
    def validate_medium(cls, v: str) -> str:
        if v not in PORT_MEDIA:
            raise ValueError(f"medium must be one of {list(PORT_MEDIA)}")
        return v

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        if v not in PORT_DIRECTIONS:
            raise ValueError(f"direction must be one of {list(PORT_DIRECTIONS)}")
        return v

    @model_validator(mode="after")
    def validate_range(self) -> "PortTemplateGroup":
        if not 0 <= self.start <= 4095:
            raise ValueError("start must be between 0 and 4095")
        if not 1 <= self.count <= 256:
            raise ValueError("count must be between 1 and 256")
        if self.start + self.count - 1 > 4095:
            raise ValueError("start + count - 1 must not exceed 4095")
        if "{slot}" in self.name and not self.slots:
            raise ValueError("a name using {slot} requires slots")
        return self


class PortSpec(NamedTuple):
    slot: Optional[int]
    number: int
    name: str
    medium: str
    direction: str


def expand_port_template(groups) -> List[PortSpec]:
    """Pure expansion of a port template (PortTemplateGroup objects or the raw
    JSON dicts stored on device_type) into one PortSpec per port.

    Mirrored by frontend-erp for the live preview only; the backend's
    sync_item_ports reports what it actually applied."""
    specs: List[PortSpec] = []
    for group in groups or []:
        if not isinstance(group, PortTemplateGroup):
            group = PortTemplateGroup.model_validate(group)
        for slot in group.slots or [None]:
            for n in range(group.start, group.start + group.count):
                name = group.name.replace("{slot}", str(slot)).replace("{n}", str(n))
                specs.append(PortSpec(slot, n, name, group.medium, group.direction))
    return specs


def validate_port_template(
    groups: Optional[List[PortTemplateGroup]],
) -> Optional[List[PortTemplateGroup]]:
    """List-level rules: group and port caps, the name character rule, and the
    two identities (name case-insensitively; PON on slot/number/direction).
    An empty list means "no template" and is stored as NULL."""
    if not groups:
        return None
    if len(groups) > MAX_TEMPLATE_GROUPS:
        raise ValueError(f"port_template allows at most {MAX_TEMPLATE_GROUPS} groups")
    if sum(len(g.slots or [None]) * g.count for g in groups) > MAX_TEMPLATE_PORTS:
        raise ValueError(f"port_template expands to more than {MAX_TEMPLATE_PORTS} ports")
    names, pon = set(), set()
    for spec in expand_port_template(groups):
        if not _PORT_NAME.fullmatch(spec.name):
            raise ValueError(
                f"port name '{spec.name}' must match {PORT_NAME_PATTERN} (no quotes, "
                "braces or newlines; it reaches device CLIs)"
            )
        if spec.name.lower() in names:
            raise ValueError(f"port name '{spec.name}' is repeated (names are case-insensitive)")
        names.add(spec.name.lower())
        if spec.medium == "PON":
            identity = (spec.slot, spec.number, spec.direction)
            if identity in pon:
                raise ValueError(
                    f"PON port slot {spec.slot} number {spec.number} {spec.direction} is repeated"
                )
            pon.add(identity)
    return groups


def normalize_path_role(v: Optional[str]) -> Optional[str]:
    """path_role (doc 40 §3.1.2): ^[a-z][a-z0-9_]{0,31}$ and not secret-named.
    PATH_ROLE_SHADOWS_CATEGORY needs the DB: see path_role_shadows_category."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if not _PATH_ROLE.fullmatch(v):
        raise ValueError(f"path_role must match {PATH_ROLE_PATTERN}")
    if is_secret_name(v):
        raise ValueError(f"path_role '{v}' is secret-named")
    return v


def path_role_shadows_category(db, role: Optional[str]) -> bool:
    """True when `role` equals a lowercased device_category.key, i.e. it would
    collide with path.<category>.* -> the backend answers 422
    PATH_ROLE_SHADOWS_CATEGORY. Kept next to the other path_role rules so the
    resolver's runtime ROLE_SHADOWS_CATEGORY and this save-time check agree."""
    if not role:
        return False
    return db.execute(
        sa.select(DeviceCategory.id)
        .where(sa.func.lower(DeviceCategory.key) == role.lower())
        .limit(1)
    ).first() is not None


_PORT_TEMPLATE_REQUIRES_SERIALIZED = (
    "PORT_TEMPLATE_REQUIRES_SERIALIZED: lot (non-serialized) types have no physical ports"
)


# --- DeviceType ---

class DeviceTypeBase(BaseModel):
    name: str
    # Cycle 3 E4: device_category.key (a plain string, server-resolved and
    # validated against the device_category table) — replaces the
    # `devicecategory` PG enum (dropped in revision c3b_device_categories).
    # Unknown keys previously 422'd via FastAPI enum coercion; the router
    # must now resolve the key -> id explicitly and 422 on unknown/inactive.
    category: str = 'OTHER'
    vendor: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    attribute_schema: Optional[List[AttributeDefinition]] = None
    default_attributes: Optional[Dict[str, Any]] = None
    # Cycle 5 Phase 1 (canon C6): device-group provisioning opt-out gate.
    provisioning_enabled: bool = True
    # Cycle 7 (doc 25 §2.2): netmiko platform id for the generic CLI drivers;
    # NULL -> 'generic' / 'generic_telnet'. Free string (open netmiko set).
    cli_platform: Optional[str] = None
    # Figma redesign PR 8 (08-inventario §2.2): serialized gear is one row per
    # physical unit (serial required); non-serialized types are lots counted by
    # `inventory_item.quantity` and displayed in `unit` ("m", "u", "pz").
    is_serialized: bool = True
    unit: Optional[str] = None
    # doc 40 §3.1.2: generated ports and the per-company path role.
    port_template: Optional[List[PortTemplateGroup]] = None
    path_role: Optional[str] = None

    @field_validator("port_template")
    @classmethod
    def check_port_template(cls, v):
        return validate_port_template(v)

    @field_validator("path_role")
    @classmethod
    def check_path_role(cls, v):
        return normalize_path_role(v)

    @model_validator(mode="after")
    def validate_ports_serialized(self) -> "DeviceTypeBase":
        if self.port_template and not self.is_serialized:
            raise ValueError(_PORT_TEMPLATE_REQUIRES_SERIALIZED)
        return self


class DeviceTypeCreate(DeviceTypeBase):
    pass


class DeviceTypeUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    attribute_schema: Optional[List[AttributeDefinition]] = None
    default_attributes: Optional[Dict[str, Any]] = None
    provisioning_enabled: Optional[bool] = None
    cli_platform: Optional[str] = None
    is_serialized: Optional[bool] = None
    unit: Optional[str] = None
    # An explicit null (or []) clears the template; the backend reads
    # model_fields_set to tell "clear" from "not sent", and checks a template
    # sent alone against the stored is_serialized.
    port_template: Optional[List[PortTemplateGroup]] = None
    path_role: Optional[str] = None

    @field_validator("port_template")
    @classmethod
    def check_port_template(cls, v):
        return validate_port_template(v)

    @field_validator("path_role")
    @classmethod
    def check_path_role(cls, v):
        return normalize_path_role(v)

    @model_validator(mode="after")
    def validate_ports_serialized(self) -> "DeviceTypeUpdate":
        if self.port_template and self.is_serialized is False:
            raise ValueError(_PORT_TEMPLATE_REQUIRES_SERIALIZED)
        return self


class DeviceTypeOut(DeviceTypeBase):
    id: UUID
    company_id: UUID
    # Cycle 3 E4: the resolved FK id, alongside the string `category` key
    # (inherited from DeviceTypeBase, populated from the model's @property).
    category_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Warehouse ---

class WarehouseBase(BaseModel):
    name: str
    address: Optional[str] = None
    is_vehicle: bool = False
    notes: Optional[str] = None
    # mi2: map pin.
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class WarehouseCreate(WarehouseBase):
    pass


class WarehouseUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    is_vehicle: Optional[bool] = None
    notes: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class WarehouseOut(WarehouseBase):
    id: UUID
    company_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- InventoryItem ---

class InventoryItemBase(BaseModel):
    device_type_id: UUID
    serial_number: Optional[str] = None
    mac_address: Optional[str] = None
    # Cycle 5 Phase 1 (canon C13): device-identity half paired with serial for
    # the acs_device_registration match.
    oui: Optional[str] = None
    condition: InventoryItemCondition = InventoryItemCondition.NEW
    warehouse_id: Optional[UUID] = None
    attributes: Optional[Dict[str, Any]] = None
    purchase_date: Optional[datetime] = None
    warranty_until: Optional[datetime] = None
    cost: Optional[float] = None
    notes: Optional[str] = None
    # --- Cycle 7 management surface (doc 25 §2.3) — how the CLI drivers reach
    # a CORE-tier device. mgmt_port NULL -> driver default (22 ssh / 23 telnet).
    mgmt_host: Optional[str] = None
    mgmt_port: Optional[int] = Field(default=None, ge=1, le=65535)
    cli_protocol: Optional[str] = None
    # spec N1: the external port on the tenant's gateway that dst-nats to this
    # device. Never conflated with mgmt_port, which stays the device's real
    # service port.
    nat_port: Optional[int] = Field(default=None, ge=1, le=65535)
    # --- Figma redesign PR 8 (08-inventario §2.3) ---
    # Lot size. Always 1 for a serialized device type (the router 422s
    # QUANTITY_NOT_ALLOWED otherwise); >1 only for consumables.
    quantity: int = Field(default=1, ge=1)
    # Display name for plant with no serial ("MUFA 1", "Router 563").
    label: Optional[str] = None
    # Current custody ("Con tecnico"). Ownership-checked by the router.
    custodian_user_id: Optional[UUID] = None
    # Integer cents, the money convention. `cost` (Float) stays accepted for
    # the mobile app; the router mirrors one into the other.
    cost_cents: Optional[int] = None

    @field_validator("cli_protocol")
    @classmethod
    def validate_cli_protocol(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_cli_protocol(v)


class InventoryItemCreate(InventoryItemBase):
    # The create form assigns the client and the service in the same call.
    client_id: Optional[UUID] = None
    client_service_id: Optional[UUID] = None


class InventoryItemUpdate(BaseModel):
    serial_number: Optional[str] = None
    mac_address: Optional[str] = None
    oui: Optional[str] = None
    status: Optional[InventoryItemStatus] = None
    condition: Optional[InventoryItemCondition] = None
    warehouse_id: Optional[UUID] = None
    client_id: Optional[UUID] = None
    client_service_id: Optional[UUID] = None
    attributes: Optional[Dict[str, Any]] = None
    purchase_date: Optional[datetime] = None
    warranty_until: Optional[datetime] = None
    cost: Optional[float] = None
    notes: Optional[str] = None
    # Cycle 7 (doc 25 §2.3): mgmt_* are PATCHable through the existing
    # inventory PATCH (no dedicated endpoint); the mgmt_last_check_* stamps
    # are deliberately absent — worker-owned, read-only.
    mgmt_host: Optional[str] = None
    mgmt_port: Optional[int] = Field(default=None, ge=1, le=65535)
    cli_protocol: Optional[str] = None
    nat_port: Optional[int] = Field(default=None, ge=1, le=65535)
    quantity: Optional[int] = Field(default=None, ge=1)
    label: Optional[str] = None
    custodian_user_id: Optional[UUID] = None
    cost_cents: Optional[int] = None
    # mi2: field geolocation (plant: MUFA / NAP).
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    gps_precision_m: Optional[float] = Field(default=None, ge=0)

    @field_validator("cli_protocol")
    @classmethod
    def validate_cli_protocol(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_cli_protocol(v)


class InventoryItemOut(InventoryItemBase):
    id: UUID
    company_id: UUID
    status: InventoryItemStatus
    client_id: Optional[UUID] = None
    client_service_id: Optional[UUID] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    device_type: Optional[DeviceTypeOut] = None
    warehouse: Optional[WarehouseOut] = None
    # --- Figma redesign PR 8 (08-inventario §2.3/§3.2) ---
    # Plant-tree position (columns) + the flattened names the item sheet reads
    # (backend-computed from OUTER joins, so every one of them is optional and
    # a bare ORM object still serializes).
    parent_id: Optional[UUID] = None
    network_attached: bool = False
    parent_label: Optional[str] = None
    custodian_name: Optional[str] = None
    client_name: Optional[str] = None
    client_address: Optional[str] = None
    warehouse_name: Optional[str] = None
    warehouse_address: Optional[str] = None
    # Derived, NEVER stored: WAREHOUSE|TECHNICIAN|CLIENT|DEPLOYED|DAMAGED|NONE,
    # computed by backend-erp utils/inventory_location.py::location_case() so
    # the /inventory/summary aggregate and the ?location= filter cannot drift.
    # Defaults to NONE so a raw ORM row still validates.
    location: str = "NONE"
    # EDGE-only ACS state (ONLINE|STALE|PRE_REGISTERED|QUARANTINED); CORE reads
    # mgmt_last_check_ok below instead, passives have neither.
    acs_state: Optional[str] = None
    acs_registration_id: Optional[UUID] = None
    # Newest equipment_event of type MAINTENANCE.
    last_maintenance_at: Optional[datetime] = None
    # Cycle 7 (doc 25 §2.3): worker-stamped connectivity-check result — read-
    # only (stamped when a core_connectivity_check job reaches terminal state).
    mgmt_last_check_at: Optional[datetime] = None
    mgmt_last_check_ok: Optional[bool] = None
    # mi2: field geolocation.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    gps_precision_m: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


# --- EquipmentEvent ---

class EquipmentEventCreate(BaseModel):
    event_type: EquipmentEventType
    notes: Optional[str] = None
    event_metadata: Optional[Dict[str, Any]] = None
    related_item_id: Optional[UUID] = None
    from_warehouse_id: Optional[UUID] = None
    to_warehouse_id: Optional[UUID] = None
    client_id: Optional[UUID] = None
    client_service_id: Optional[UUID] = None
    technician_id: Optional[UUID] = None


class EquipmentEventOut(EquipmentEventCreate):
    id: UUID
    company_id: UUID
    item_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Inventory product summary (08-inventario §3.1) ---

class InventoryProductSummaryOut(BaseModel):
    """One row of `GET /inventory/summary`: a device type ("producto") with its
    stock split by derived location. Computed by one grouped query in
    backend-erp — nothing here is a column. A device type with no items is
    returned with every count 0 (a newly created product must still appear)."""
    device_type_id: UUID
    name: str
    vendor: Optional[str] = None
    model: Optional[str] = None
    category_key: str
    category_name: str
    category_tier: Optional[str] = None
    category_icon: Optional[str] = None
    is_passive: bool = False
    is_serialized: bool = True
    unit: Optional[str] = None
    total: int = 0
    in_warehouse: int = 0
    with_technician: int = 0
    with_client: int = 0
    deployed: int = 0
    damaged: int = 0
