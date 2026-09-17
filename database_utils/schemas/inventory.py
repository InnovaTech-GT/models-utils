# schemas/inventory.py
from pydantic import BaseModel, ConfigDict, field_validator, Field
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import (
    InventoryItemStatus,
    InventoryItemCondition,
    EquipmentEventType,
    CLI_PROTOCOLS,
)


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


class WarehouseCreate(WarehouseBase):
    pass


class WarehouseUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    is_vehicle: Optional[bool] = None
    notes: Optional[str] = None


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
