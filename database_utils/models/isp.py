"""
ISP vertical models: service plans, subscriber services, network inventory,
topology, and provisioning automation.

Design rationale: docs/isp-platform/00-architecture-decisions.md (repo root),
docs/isp-platform/18-cycle2-design.md (D1-D10, entity merge + topology rework).
- Hybrid inventory (ADR-002): hot fields as columns, vendor long-tail in
  schema-validated JSONB (`attributes` validated against the catalog's
  `attribute_schema`).
- Topology as an ordered device-type chain (D5, Cycle 2): the free-form
  network graph (network_node/network_node_type/network_link, ADR-003) was
  removed in revision c2d_graph_removal — device kinds are resolved by TYPE
  from a client's assigned inventory, not by a mapped node graph.
- Durable provisioning queue (ADR-005): provisioning_job rows are claimed by
  the worker via SELECT ... FOR UPDATE SKIP LOCKED.
"""
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, JSON, DateTime, ForeignKey, Enum, text,
    Uuid, Float, Index, UniqueConstraint, CheckConstraint, LargeBinary,
    SmallInteger, ForeignKeyConstraint,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column, validates

from database_utils.database import Base
from ..utils.timezone_utils import now_gt, make_aware_gt
# Cycle 2 D1 (entity merge): client_service billing reuses these EXISTING PG
# enum types owned by recurring_order — zero new-enum risk (doc 18 §1b).
from .crm import RecurrenceEnum, RecurringOrderStatus

import enum
import uuid
from typing import Optional


# ---------------------------------------------------------------------------
# Enums (only for closed, state-machine-like sets; open sets are config rows)
# ---------------------------------------------------------------------------

class ServicePlanType(str, enum.Enum):
    FIBER = "FIBER"
    CABLE = "CABLE"
    WIRELESS = "WIRELESS"
    DSL = "DSL"
    OTHER = "OTHER"


class CatalogKind(str, enum.Enum):
    """Cycle 2 entity merge (D1/D2): what a ServicePlan bills FOR. Drives
    Order.order_type derivation (utils/order_typing.py) — INSTALLATION-kind
    items make an order an install work order regardless of plan_type."""
    SERVICE = "SERVICE"
    INSTALLATION = "INSTALLATION"
    HARDWARE = "HARDWARE"


class ClientServiceStatus(str, enum.Enum):
    PENDING_INSTALL = "PENDING_INSTALL"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class SuspensionReason(str, enum.Enum):
    NON_PAYMENT = "NON_PAYMENT"
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"
    MAINTENANCE = "MAINTENANCE"
    FRAUD = "FRAUD"
    OTHER = "OTHER"


# Cycle 3 E4 (revision c3b_device_categories): the `devicecategory` PG enum
# that used to live here is GONE — DeviceCategory is now a table-backed model
# (see the "Inventory" section below, right before DeviceType, which is its
# first consumer). The class name is deliberately reused so any stray
# pre-Cycle-3 usage (e.g. `DeviceCategory.OTHER`) fails loudly at import/
# attribute-access time instead of silently degrading.

# Canonical playbook purposes (Cycle 3 E1; re-homed onto device-type bindings
# in Cycle 10, doc 35 §2.4). Plain strings, NOT a PG enum — tenants may define
# custom purposes (uppercase snake, CHECK-enforced on the binding tables).
# Integrity is enforced instead by a DB CHECK constraint, the per-binding
# UNIQUE, and the shared Pydantic normalizer (schemas/playbook.py:
# strip -> upper -> regex). These constants are the single source of truth
# shared by models, the workflow engine (ENQUEUE_PROVISIONING use_service_path
# mode), and seeds.
PURPOSE_ACTIVATION = 'ACTIVATION'
PURPOSE_SUSPENSION = 'SUSPENSION'
PURPOSE_REACTIVATION = 'REACTIVATION'
PURPOSE_DEPROVISION = 'DEPROVISION'
CANONICAL_PLAYBOOK_PURPOSES = (
    PURPOSE_ACTIVATION, PURPOSE_SUSPENSION, PURPOSE_REACTIVATION, PURPOSE_DEPROVISION
)
PLAYBOOK_PURPOSE_PATTERN = r'^[A-Z][A-Z0-9_]{0,49}$'

# Service-lifecycle cycle: the status machine behind the per-purpose
# lifecycle actions (activate / suspend / reactivate / cancel). Lives HERE, not
# in backend-erp, because the workflow engine resolves the same purposes
# (import direction is strictly downward — see CLAUDE.md).
#
# ACTIVATION maps to None ON PURPOSE (founder decision 5): activating a service
# ENQUEUES ONLY. The status flip PENDING_INSTALL -> ACTIVE is written by
# recompute_install_state (backend-erp/utils/install_state.py) once the install
# state reaches INSTALLED — i.e. after the provisioning job succeeds. Encoding
# it as None means a caller doing `new_status = PURPOSE_TO_STATUS[purpose]`
# cannot accidentally short-circuit that and mark a service ACTIVE before the
# network agrees.
PURPOSE_TO_STATUS = {
    PURPOSE_ACTIVATION: None,
    PURPOSE_SUSPENSION: ClientServiceStatus.SUSPENDED,
    PURPOSE_REACTIVATION: ClientServiceStatus.ACTIVE,
    PURPOSE_DEPROVISION: ClientServiceStatus.CANCELLED,
}

# Legal status writes. CANCELLED is terminal — a cancelled service is never
# revived (re-selling is a NEW client_service row); DELETE additionally refuses
# any non-cancelled service, so cancel is the only way out.
ALLOWED_TRANSITIONS = {
    ClientServiceStatus.PENDING_INSTALL: {ClientServiceStatus.ACTIVE, ClientServiceStatus.CANCELLED},
    ClientServiceStatus.ACTIVE: {ClientServiceStatus.SUSPENDED, ClientServiceStatus.CANCELLED},
    ClientServiceStatus.SUSPENDED: {ClientServiceStatus.ACTIVE, ClientServiceStatus.CANCELLED},
    ClientServiceStatus.CANCELLED: set(),
}


def purpose_allowed_for_status(purpose, current_status) -> bool:
    """Is this lifecycle action legal against a service in `current_status`?

    Single source of truth for both the backend pre-flight gate and the
    frontend's per-purpose action buttons (founder decision 3: a button is
    enabled only when the machine allows it AND a playbook exists for that
    purpose — this answers the first half).

    Four cases:
      1. ACTIVATION — special-cased, because it writes no status and therefore
         has no target to look up in ALLOWED_TRANSITIONS. Legal ONLY from
         PENDING_INSTALL: an already-ACTIVE service gets no Activate button,
         and re-activating a SUSPENDED service is REACTIVATION's job.
      2. REACTIVATION — ALSO special-cased, and for a reason that is not
         obvious: it is the exact inverse of SUSPENSION, so it is legal ONLY
         from SUSPENDED. The generic rule (case 3) would wrongly allow it from
         PENDING_INSTALL, because its target ACTIVE happens to be a legal
         transition out of PENDING_INSTALL — that edge belongs to ACTIVATION
         and is owned by recompute_install_state, NOT by a status write. Taking
         the generic path there would mark a never-installed service ACTIVE and
         start billing it (status/activation_date/billing_status/
         next_generation_date all written) for an install that never happened
         and a CPE that was never linked. The legacy
         `POST /client-services/{id}/reactivate` endpoint has always rejected
         this ("Only SUSPENDED services can be reactivated"); both paths share
         `_apply_reactivation`, so they must agree.
      3. Any other canonical purpose — legal iff its target status is a legal
         transition out of `current_status`.
      4. A tenant-defined custom purpose (purposes are extensible free strings,
         see PLAYBOOK_PURPOSE_PATTERN) — falls through to True: it is
         enqueue-only, writes no status, so there is no transition to police.
         It MUST NOT raise; an unknown purpose is a normal tenant config, not a
         bug.

    Accepts `current_status` as a ClientServiceStatus or its string value, and
    `purpose` in any case/spacing the normalizer would accept.
    """
    if purpose is None:
        return False
    key = str(getattr(purpose, 'value', purpose)).strip().upper().replace(' ', '_').replace('-', '_')

    try:
        status = ClientServiceStatus(getattr(current_status, 'value', current_status))
    except ValueError:
        return False

    if key == PURPOSE_ACTIVATION:
        return status == ClientServiceStatus.PENDING_INSTALL

    if key == PURPOSE_REACTIVATION:
        # Inverse of SUSPENSION — see case 2 in the docstring. Do NOT relax
        # this to the generic ALLOWED_TRANSITIONS lookup: PENDING_INSTALL ->
        # ACTIVE is a legal edge, but it is ACTIVATION's, and reaching it here
        # bills a subscriber whose install never happened.
        return status == ClientServiceStatus.SUSPENDED

    if key not in PURPOSE_TO_STATUS:
        return True  # custom purpose: enqueue-only, no status transition

    target = PURPOSE_TO_STATUS[key]
    if target is None:
        return False  # unreachable today; a future None mapping is not a write
    return target in ALLOWED_TRANSITIONS.get(status, set())


class InventoryItemStatus(str, enum.Enum):
    IN_STOCK = "IN_STOCK"
    RESERVED = "RESERVED"
    INSTALLED = "INSTALLED"
    IN_REPAIR = "IN_REPAIR"
    RETIRED = "RETIRED"
    LOST = "LOST"


class InventoryItemCondition(str, enum.Enum):
    NEW = "NEW"
    USED = "USED"
    REFURBISHED = "REFURBISHED"
    DAMAGED = "DAMAGED"


class EquipmentEventType(str, enum.Enum):
    RECEIVED = "RECEIVED"
    TRANSFERRED = "TRANSFERRED"
    RESERVED = "RESERVED"
    INSTALLED = "INSTALLED"
    REPLACED = "REPLACED"
    REMOVED = "REMOVED"
    REPAIRED = "REPAIRED"
    RETIRED = "RETIRED"
    MAINTENANCE = "MAINTENANCE"
    # mi1: a lot (non-serialized) was used up on a task's materials.
    CONSUMED = "CONSUMED"
    # mi1: a reservation was undone — back to IN_STOCK, custody kept.
    RELEASED = "RELEASED"


class ProvisioningJobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"
    # Cycle 5 Phase 1 (network config, canon C2, revision nc1a): the job parks
    # when a TR-069 connection-request task returns 202; the worker slot is
    # released and a poller settles the parked step (pending_step_index) once
    # the inform arrives. Added to the PG enum via ALTER TYPE ADD VALUE in an
    # autocommit block (nc1a) — see the migration docstring.
    PENDING_INFORM = "PENDING_INFORM"


class ProvisioningTrigger(str, enum.Enum):
    USER = "USER"
    WORKFLOW = "WORKFLOW"
    API = "API"


# ---------------------------------------------------------------------------
# Cycle 5 Phase 1 (network configuration / GenieACS-TR069). Plan:
# docs/isp-platform/23-network-config-implementation-plan.md §2; canonical
# conventions register C1–C20. Open, driver-bounded value sets are
# CHECK-constrained strings, NOT PG enums (c3a/c3b precedent) — adding a value
# is a plain transactional ALTER of the CHECK constraint, never the
# ALTER TYPE ... ADD VALUE autocommit dance.
# ---------------------------------------------------------------------------

# canon C19: credential kinds — an OPEN set bounded by driver support (grows by
# phase: tr069 P1; ssh/telnet/snmp P2; agent protocols P3).
CREDENTIAL_KINDS = (
    "SSH", "TELNET", "SNMP_COMMUNITY", "TR069_CONNECTION_REQUEST",
    "HTTP_BASIC", "HTTP_BEARER", "WIREGUARD", "AGENT",
)

# tr1_transport_axis: the transport is TWO orthogonal per-tenant choices, not one
# cross-product enum. `dial_target` answers "whose address do we dial", and
# `proxy_kind` answers "is there a hop, and of what sort". Both live on
# ProvisioningSettings (the tenant singleton, canon C6) — the old multi-row
# `network_access` table and its kind/mode value sets are gone.
#
#   device  + none    devices have public IPs          (was mode 'direct')
#   gateway + none    NAT + port map to a public IP    (was mode 'nat_public')
#   gateway + socks5  NAT + port map via ZeroTier      (was mode 'nat_zt')
#   device  + socks5  hub + managed routes: WireGuard, ZeroTier or any other
#                                                      (was mode 'vpn')
#
# canon C10's edge agent becomes proxy_kind='agent', not a new mode.
DIAL_TARGETS = ("device", "gateway")
PROXY_KINDS = ("none", "socks5")

# canon C13: derived acs_device_registration ONLINE-vs-STALE threshold (a
# registration that has not informed within this window reads STALE).
ACS_STALE_AFTER_SECONDS = 900

# SQL fragments reused by both the model CheckConstraints below and the
# hand-written nc1a migration — kept as strings so both agree byte-for-byte.
_CREDENTIAL_KIND_CHECK = "kind IN ('SSH','TELNET','SNMP_COMMUNITY','TR069_CONNECTION_REQUEST','HTTP_BASIC','HTTP_BEARER','WIREGUARD','AGENT')"
# spec §8: mgmt_port has had no range CHECK since nc2a and the xlsx importer
# will happily write 0 or 70000. Both ports get one here.
_NAT_PORT_CHECK = "nat_port IS NULL OR (nat_port BETWEEN 1 AND 65535)"
_MGMT_PORT_CHECK = "mgmt_port IS NULL OR (mgmt_port BETWEEN 1 AND 65535)"
# Figma redesign PR 8 (08-inventario §2.3): a lot row is at least one unit.
# Zero is not "out of stock", it is a row that should have been deleted.
_INVENTORY_QUANTITY_CHECK = "quantity >= 1"
# tr1_transport_axis: the transport axis on provisioning_settings. Shared
# byte-for-byte with the hand-written tr1 migration; tests/test_transport_axis.py
# pins them equal.
#
# proxy_kind is LOAD-BEARING and deliberately NOT collapsed into
# "proxy_address IS NOT NULL": dial_target='device' with no proxy is the
# legitimate public-IP case, so without an explicit intent value the resolver
# could not tell "no proxy needed" from "a hub is intended but its address is
# missing" — and the second would silently dial an RFC1918 address from the
# Railway container. That is the canon R23 fail-closed guarantee.
_PROVISIONING_DIAL_TARGET_CHECK = "dial_target IN ('device','gateway')"
_PROVISIONING_PROXY_KIND_CHECK = "proxy_kind IN ('none','socks5')"
_PROVISIONING_PROXY_ADDRESS_CHECK = (
    "proxy_kind <> 'socks5' OR proxy_address IS NOT NULL"
)
_PROVISIONING_GATEWAY_HOST_CHECK = (
    "dial_target <> 'gateway' OR gateway_host IS NOT NULL"
)
# The accept-both rotation window is two FKs, so "the pending secret is not the
# current one" is expressible. "No pending without a current" is NOT: both FKs
# are ON DELETE SET NULL, so deleting the current credential mid-window would
# violate such a CHECK through a referential action and turn an ordinary DELETE
# into a 500. That half is a 409 in backend-erp's router.
_PROVISIONING_CWMP_PAIR_CHECK = (
    "cwmp_pending_credential_id IS NULL "
    "OR cwmp_credential_id <> cwmp_pending_credential_id"
)

# ---------------------------------------------------------------------------
# Cycle 7 (core network configuration, doc 25 §2, revision nc2a_core_config).
# Same c3a/c3b/nc1a precedent: every new value set is a CHECK-constrained
# string, never a PG enum. The SQL fragments below are shared byte-for-byte
# with the hand-written nc2a migration (the _CREDENTIAL_KIND_CHECK pattern).
# ---------------------------------------------------------------------------

# doc 25 §2.1: the CORE/EDGE axis on device categories. CORE = shared
# infrastructure devices (one physical device serves many subscribers, pinned
# per topology position via topology_device_type.inventory_item_id); EDGE =
# per-subscriber CPE resolved from the client's assigned inventory. NULL =
# passives/unclassified (splitters, patch panels, ...).
# Figma redesign PR 8 (docs/design/plans/08-inventario.md §2.1): the axis grows
# past network gear. CONSUMABLE (patch cords, fiber, distribution boxes), TOOL
# (fusion splicers, scanners) and OTHER (SIM cards) are what "Inventario
# general" means — they are stocked and issued to technicians but never sit on
# a configuration path. CORE/EDGE keep their doc-25 meaning exactly; NULL still
# means passive/unclassified plant (splitters, MUFAs, patch panels).
DEVICE_CATEGORY_TIERS = ("CORE", "EDGE", "CONSUMABLE", "TOOL", "OTHER")

# doc 25 §2.3: transports the generic netmiko CLI drivers speak (Phase 2).
# Lowercase on purpose — these are driver keys, matching the playbook step
# `driver` values, not display strings.
CLI_PROTOCOLS = ("ssh", "telnet")

# doc 25 §2.5: subscriber install state machine on client_service — separate
# from billing `status` by founder decision (2026-07-17 #2). Monotonic upward
# except unlink-cpe may regress to NOT_INSTALLED; first INSTALLED stamps
# installed_at and auto-transitions status PENDING_INSTALL -> ACTIVE
# (backend-erp utils/install_state.py owns the recompute).
INSTALL_STATES = ("NOT_INSTALLED", "IN_PROGRESS", "INSTALLED")

# NOTE: the pre-inv1 fragment ("tier IN ('CORE','EDGE')") is frozen byte-for-byte
# inside nc2a_core_config.py — revisions are immutable, so inv1 drops and
# recreates the constraint instead of editing that file.
_DEVICE_CATEGORY_TIER_CHECK = (
    "tier IN ('CORE','EDGE','CONSUMABLE','TOOL','OTHER')"
)
_CLI_PROTOCOL_CHECK = "cli_protocol IN ('ssh','telnet')"

# doc 40 §3.1 (revision pt1_port_topology): port-level topology. Shared
# byte-for-byte with the hand-written pt1 migration.
PORT_MEDIA = ("ETH", "PON")
PORT_DIRECTIONS = ("UP", "DOWN", "ANY")
PORT_ORIGINS = ("TEMPLATE", "ITEM")          # ITEM = per-item addition
NETWORK_LINK_SOURCES = ("OFFICE", "FIELD", "IMPORT")
# Port names reach device CLIs through `out_port_name`, so no quotes, braces
# or newlines. Same rule for template-expanded and per-item ports.
PORT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9/:._ -]{0,31}$"
PATH_ROLE_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_DEVICE_TYPE_PORTS_SERIALIZED_CHECK = "port_template IS NULL OR is_serialized"
_INSTALL_STATE_CHECK = "install_state IN ('NOT_INSTALLED','IN_PROGRESS','INSTALLED')"

# ba1 (doc 30): values of the backend-computed ClientServiceOut.activation_evidence
# derived field ('provisioned' = a SUCCEEDED non-dry-run activation job exists;
# 'attested' = no real job, adopted_at is the ACTIVE reason for INSTALLED;
# None = neither). Not a DB column — computed at serialization in backend-erp.
ACTIVATION_EVIDENCE_PROVISIONED = "provisioned"
ACTIVATION_EVIDENCE_ATTESTED = "attested"
ACTIVATION_EVIDENCE_VALUES = (ACTIVATION_EVIDENCE_PROVISIONED, ACTIVATION_EVIDENCE_ATTESTED)


class InsightChartType(str, enum.Enum):
    NUMBER = "NUMBER"
    BAR = "BAR"
    PIE = "PIE"
    LINE = "LINE"  # Insights v2 (iv1_insights_v2)


# ---------------------------------------------------------------------------
# Service catalog & subscriptions
# ---------------------------------------------------------------------------

class ServicePlan(Base):
    __tablename__ = "service_plan"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    plan_type = Column(Enum(ServicePlanType), nullable=False, default=ServicePlanType.FIBER)
    download_mbps = Column(Integer, nullable=True)
    upload_mbps = Column(Integer, nullable=True)
    data_cap_gb = Column(Integer, nullable=True)  # NULL = unlimited
    price = Column(Float, nullable=False, default=0.0)
    # Money-in-cents shadow column (Cycle 1 dual-write; Float `price` drops in
    # Cycle 2). Nullable, no server_default — doc 16 §1/§2.2.
    price_cents = Column(BigInteger, nullable=True)
    # cfg2: the Figma "Servicio" grouping label ("Fibra óptica", "Cable HFC").
    # A group's status is derived (active = any(plan.is_active)), so it owns no
    # attributes of its own.
    # ponytail: free-text label, no service_offering table — promote to one the
    # day a service needs its own price/description/contract terms.
    service_group = Column(String(100), nullable=True)
    # Integer cents, like every other money column. NULL or 0 => "Gratis".
    installation_price_cents = Column(BigInteger, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    # Vendor-agnostic provisioning intent consumed as playbook variables
    # (doc 33). Rows: [{"key", "value", "description", "scope"}] where scope is
    # 'plan' (default — one shared value for every service on this plan) or
    # 'service' (the plan DECLARES the parameter; each ClientService supplies
    # its own value in client_service.provisioning_params). Both reach a
    # playbook as {{service_plan.<key>}}, so flipping a parameter's scope never
    # requires editing a playbook.
    provisioning_params = Column(JSON, nullable=True)
    # Cycle 2 entity merge (D1/D2): what this plan bills for. NOT NULL with a
    # server_default so the additive c2a migration never blocks on existing
    # rows; drives Order.order_type derivation (utils/order_typing.py).
    kind = Column(
        Enum(CatalogKind), nullable=False,
        default=CatalogKind.SERVICE, server_default='SERVICE'
    )
    # Absorbed from Product (Cycle 2 D1). NULL = not stock-tracked (SERVICE/
    # INSTALLATION plans typically have no stock concept).
    stock = Column(Integer, nullable=True)
    # Dedicated migration marker (doc 18 amendment 1) — NEVER a JSON field,
    # NEVER exposed on Update schemas. 'c2a' = inserted by the c2a backfill.
    # Used by c2a's downgrade to delete ONLY rows it created.
    migration_source = Column(String, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )

    company = relationship("Company", back_populates="service_plans")
    client_services = relationship("ClientService", back_populates="service_plan")

    __table_args__ = (
        # cfg2: grouped listing + the ?service_group= filter, always company-scoped.
        Index("ix_service_plan_company_group", "company_id", "service_group"),
    )


class ClientService(Base):
    """A subscriber's service instance: client x plan x network attachment."""
    __tablename__ = "client_service"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    status = Column(
        Enum(ClientServiceStatus), nullable=False,
        default=ClientServiceStatus.PENDING_INSTALL, server_default='PENDING_INSTALL'
    )
    activation_date = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    # Connection long-tail: {"pppoe_user": "...", "static_ip": "...", "onu_port": 2}
    # Free-form and NOT a playbook variable source — see provisioning_params
    # below for the declared, per-service parameter values.
    connection_params = Column(JSON, nullable=True)
    # Per-service VALUES for the parameters this service's plan declares with
    # scope='service' (doc 33 follow-up). Shape: [{"key": ..., "value": ...}].
    # The plan owns the DECLARATION (key/description/scope); the service owns
    # only the value, so a playbook references both shared and per-service
    # parameters as {{service_plan.<key>}} and never has to change when a
    # parameter's scope flips.
    provisioning_params = Column(JSON, nullable=True)
    notes = Column(String, nullable=True)
    # Cycle 5 Phase 1 (functionality F1.4/F2.3, revision nc1a): learned network
    # identifiers written by the provisioning executor at job settlement
    # ({"ont_id": ..., "service_port_ids": [...], "vlan": ..., ...}). Read back
    # as template variables by suspension/reactivation/deprovision playbooks —
    # without it, teardown cannot know which service-ports to delete. JSON:
    # shape varies by vendor/topology, read whole at render time (ADR-002).
    provisioning_state = Column(JSON, nullable=True)
    # Cycle 7 (doc 25 §2.5, revision nc2a_core_config): install state machine,
    # deliberately SEPARATE from billing `status` (founder decision 2026-07-17
    # #2). CHECK-constrained string (INSTALL_STATES), never a PG enum. Written
    # exclusively by backend-erp's recompute_install_state — routers/workflows
    # must not PATCH it directly (not exposed on Update schemas).
    install_state = Column(
        String(20), nullable=False,
        default=INSTALL_STATES[0], server_default='NOT_INSTALLED'
    )
    # Stamped on the FIRST transition to INSTALLED (never cleared by a later
    # regression to NOT_INSTALLED — a historical fact, like activation_date).
    installed_at = Column(DateTime(timezone=True), nullable=True)
    # Cycle 10 (doc 35 §5.2): set when someone re-parented a node above this
    # service's CPE, so the configuration path it was provisioned against is no
    # longer the path it sits on. Cleared by a SUCCEEDED non-dry-run ACTIVATION
    # run. This NEVER triggers provisioning on its own — pushing config to live
    # carrier gear as a side effect of an org-chart edit is the wrong blast
    # radius; the operator confirms.
    path_changed_at = Column(DateTime(timezone=True), nullable=True)
    # Brownfield adoption (doc 30, revision ba1_attested_adoption): a
    # persistent ATTESTATION FACT substituting for the missing SUCCEEDED
    # activation job in install_state derivation (backend-erp
    # utils/install_state.py _activation_ok checks real job evidence FIRST,
    # adopted_at second). Never creates ProvisioningJob rows, never touches
    # devices, never written together with install_state — install_state
    # stays exclusively recompute_install_state's output. NEVER exposed on
    # Create/Update schemas (migration_source precedent); written only by
    # the adopt/un-adopt endpoints behind client_services.adopt.
    adopted_at = Column(DateTime(timezone=True), nullable=True)
    adoption_note = Column(String, nullable=True)

    # --- Cycle 2 D1 billing absorption (client_service absorbs recurring_order) ---
    # NULL recurrence/billing_status = billing not configured on this service
    # (legacy/unmigrated or a non-billed attachment). Reuses the EXISTING
    # recurrenceenum/recurringorderstatus PG enum types owned by recurring_order
    # — zero new-enum risk, values copied verbatim (doc 18 amendment, §1b).
    recurrence = Column(Enum(RecurrenceEnum), nullable=True)
    recurrence_end = Column(DateTime(timezone=True), nullable=True)
    # The billing anchor: next charge date. Cycle arithmetic keys off this
    # exactly as RecurringOrderService does today.
    next_generation_date = Column(DateTime(timezone=True), nullable=True)
    last_generated_at = Column(DateTime(timezone=True), nullable=True)
    billing_status = Column(Enum(RecurringOrderStatus), nullable=True)
    quantity = Column(Integer, nullable=False, default=1, server_default='1')
    # Dedicated migration marker (doc 18 amendment 1) — NEVER JSON (the old
    # connection_params-based marker was user-writable and got clobbered by
    # whole-object PATCHes). NEVER exposed on Update schemas. 'c2b' = a
    # client_service materialized by the c2b Pass-2 backfill.
    migration_source = Column(String, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT: a plan with live subscriptions cannot be deleted (history matters).
    service_plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_plan.id", ondelete="RESTRICT"), nullable=False
    )
    # Cycle 10 (doc 35 §2.5): the subscriber's edge device. This and the item's
    # own `parent_id` are the ONLY two network inputs a service takes; the whole
    # configuration path is derived by walking the graph from here to the root.
    # SET NULL rather than RESTRICT: an RMA'd ONT should not block deleting the
    # inventory row, and a service without a CPE is a legible state (it simply
    # cannot be provisioned, reported as CPE_NOT_SET).
    cpe_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True
    )
    # SET NULL: deleting the attesting user must not erase the attestation
    # fact (adopted_at/adoption_note survive; only authorship is lost) —
    # mirrors ServiceSuspension.created_by (isp.py:410-415).
    adopted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    company = relationship("Company", back_populates="client_services")
    client = relationship("Client", back_populates="services")
    service_plan = relationship("ServicePlan", back_populates="client_services")
    suspensions = relationship(
        "ServiceSuspension", back_populates="client_service", cascade="all, delete-orphan"
    )
    # Two FK paths now join these tables: this one (items assigned to a service)
    # and cpe_item_id (the one item that IS the service's edge). Both are named
    # explicitly or SQLAlchemy cannot pick a join condition.
    equipment = relationship(
        "InventoryItem", back_populates="client_service",
        foreign_keys="InventoryItem.client_service_id",
    )
    cpe_item = relationship("InventoryItem", foreign_keys=[cpe_item_id])
    adopted_by = relationship("User", foreign_keys=[adopted_by_user_id])

    __table_args__ = (
        Index("ix_client_service_company_status", "company_id", "status"),
        Index("ix_client_service_cpe_item_id", "cpe_item_id"),
        # The cron due-scan replacement for ix_recurring_order_status_company
        # (revision c2b_service_billing).
        Index(
            "ix_client_service_billing_due",
            "billing_status", "company_id", "next_generation_date",
        ),
        # Cycle 7 (doc 25 §2.5): services-page install-state badge filter scan.
        Index("ix_client_service_company_install_state", "company_id", "install_state"),
        # ba1: adoption-campaign scans + the adoption-template export
        # (services lacking adoption). Partial — adopted rows are a small
        # minority forever (uq_service_plan_product postgresql_where precedent).
        Index(
            "ix_client_service_adopted", "company_id",
            postgresql_where=text("adopted_at IS NOT NULL"),
        ),
        CheckConstraint(_INSTALL_STATE_CHECK, name="ck_client_service_install_state"),
    )

    @validates("status")
    def _stamp_status_dates(self, key, value):
        """Domain rule enforced at the model so every writer (router, workflow
        engine UPDATE_FIELD, worker) behaves identically: first transition to
        ACTIVE stamps activation_date; CANCELLED stamps cancelled_at."""
        new_status = value.value if isinstance(value, ClientServiceStatus) else value
        if new_status == ClientServiceStatus.ACTIVE.value and self.activation_date is None:
            self.activation_date = now_gt()
        elif new_status == ClientServiceStatus.CANCELLED.value and self.cancelled_at is None:
            self.cancelled_at = now_gt()
        return value


class ServiceSuspension(Base):
    """Suspension history: one row per suspension episode (reactivated_at NULL = ongoing)."""
    __tablename__ = "service_suspension"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    suspended_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    reactivated_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(Enum(SuspensionReason), nullable=False, default=SuspensionReason.OTHER)
    note = Column(String, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    client_service = relationship("ClientService", back_populates="suspensions")
    creator = relationship("User", foreign_keys=[created_by])


# ---------------------------------------------------------------------------
# Inventory (ADR-002 hybrid)
# ---------------------------------------------------------------------------

class DeviceCategory(Base):
    """Platform-global device category (Cycle 3 E4, revision
    c3b_device_categories). Replaces the `devicecategory` PG enum with a
    super-admin-managed table (no company_id — SaaS staff own this list,
    tenants read it) so adding a category never requires a migration. `key`
    is the byte-identical successor to the old enum member names (ROUTER,
    SWITCH, ... OTHER) and is immutable after creation (enforced in
    schemas/device_category.py, not here — PG can't cheaply enforce
    immutability). `device_type.category` keeps serializing this string via a
    model @property, so API response shapes barely change across the enum->FK
    migration. (`playbook.target_category` was dropped in Cycle 8 /
    revision c8a_playbook_topology — playbooks are topology-owned now.)"""
    __tablename__ = "device_category"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default='0')
    icon = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default='true')
    is_system = Column(Boolean, nullable=False, default=False, server_default='false')
    # Cycle 7 (doc 25 §2.1, revision nc2a_core_config): the CORE/EDGE axis.
    # CHECK-constrained string (DEVICE_CATEGORY_TIERS), NULL = passives/
    # unclassified. SaaS-admin editable like name/icon (key stays immutable);
    # nc2a backfills CORE <- ROUTER/SWITCH/OLT, EDGE <- ONU/CPE_ROUTER/
    # ACCESS_POINT by key.
    tier = Column(String(10), nullable=True)
    # Cycle 10 (doc 35 §2.3, revision ng1_network_graph): signal-passive gear.
    # A passive node IS on the configuration path — it is shown, it matters for
    # troubleshooting and impact — but it is never configured.
    #
    # This is an explicit flag rather than an inference from "no playbook bound"
    # because absence of a playbook cannot distinguish "expected, it is a
    # splitter" from "someone forgot to bind an ACTIVATION playbook to this
    # OLT". The first is a calm grey chip; the second is a hard resolution
    # error. Same reasoning as `tier`: SaaS-admin editable, key stays immutable.
    is_passive = Column(Boolean, nullable=False, default=False, server_default='false')
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    __table_args__ = (
        CheckConstraint(_DEVICE_CATEGORY_TIER_CHECK, name="ck_device_category_tier"),
    )


class DeviceType(Base):
    """Per-company equipment catalog entry (vendor/model + typed attribute schema)."""
    __tablename__ = "device_type"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    # Cycle 3 E4: FK replacing the `devicecategory` enum (revision
    # c3b_device_categories, enum -> FK backfill by key match). NOT NULL —
    # every device_type had a (possibly OTHER) category under the enum.
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("device_category.id", ondelete="RESTRICT"), nullable=False
    )
    vendor = Column(String, nullable=True)   # "Huawei", "ZTE", "MikroTik", ...
    model = Column(String, nullable=True)    # "MA5800-X7", "F660", ...
    description = Column(String, nullable=True)
    # Declarative attribute definitions for items of this type:
    # [{"key": "tx_power_dbm", "label": "TX Power (dBm)", "type": "NUMBER",
    #   "required": false, "options": null, "unit": "dBm"}]
    attribute_schema = Column(JSON, nullable=True)
    default_attributes = Column(JSON, nullable=True)
    # Cycle 5 Phase 1 (canon C6, revision nc1a): the device-group provisioning
    # opt-out gate. Default TRUE — the tenant provisioning_settings row is the
    # master switch; this flag lets a tenant exclude gear it never wants
    # touched. Live jobs against a disabled type are rejected 409 at creation.
    provisioning_enabled = Column(Boolean, nullable=False, default=True, server_default='true')
    # Cycle 7 (doc 25 §2.2, revision nc2a_core_config): netmiko platform id
    # for the generic CLI drivers ('huawei_smartax', 'cisco_ios',
    # 'mikrotik_routeros', ...). NULL -> drivers fall back to 'generic' /
    # 'generic_telnet'. Free string on purpose (netmiko's platform list is an
    # open set that grows with netmiko releases — never CHECK-bound it).
    cli_platform = Column(String(50), nullable=True)
    # --- Figma redesign PR 8 (08-inventario §2.2) -----------------------------
    # Serialized gear (ONU, router, OLT) is tracked one row per physical unit
    # with a unique serial; consumables (patch cords, fiber by the metre) are
    # tracked as lots with `inventory_item.quantity`. Default TRUE: every
    # device type that exists today is serialized.
    is_serialized = Column(Boolean, nullable=False, default=True, server_default='true')
    # Display unit for non-serialized lots ("m", "u", "pz"). NULL for
    # serialized types — the unit there is always "one device".
    unit = Column(String(20), nullable=True)
    # --- port-level topology (doc 40 §3.1.2, revision pt1_port_topology) ------
    # List of port groups expanded into inventory_item_port rows for every
    # item of this type (schemas/inventory.py PortTemplateGroup +
    # expand_port_template). none_as_null: an explicit None must be SQL NULL,
    # not JSON 'null', or ck_device_type_ports_serialized rejects lot types.
    port_template = Column(JSON(none_as_null=True), nullable=True)
    # Per-company path role ("mufa_principal") emitted as path.<role>.* when
    # exactly one node on a path holds it. Not unique by design.
    path_role = Column(String(32), nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        # Lot rows have no physical ports (doc 40 §3.1.2); covers writers that
        # bypass the schema, such as the device-types xlsx sheet.
        CheckConstraint(_DEVICE_TYPE_PORTS_SERIALIZED_CHECK,
                        name="ck_device_type_ports_serialized"),
    )

    company = relationship("Company", back_populates="device_types")
    items = relationship("InventoryItem", back_populates="device_type")
    # lazy='joined': keeps list endpoints, topology chain reads, and
    # provisioning resolution free of N+1 while preserving the `.category`
    # string surface below (doc 20a E4 §2).
    category_ref = relationship("DeviceCategory", lazy="joined")

    @property
    def category(self) -> Optional[str]:
        """String key surface preserved across the enum->FK migration (Cycle
        3 E4) — routers/resolution keep reading a plain string."""
        return self.category_ref.key if self.category_ref else None


class Warehouse(Base):
    __tablename__ = "warehouse"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False)
    address = Column(String, nullable=True)
    is_vehicle = Column(Boolean, nullable=False, default=False)  # truck stock
    notes = Column(String, nullable=True)
    # mi2: map pin for the tecnicos inventory map.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )

    company = relationship("Company", back_populates="warehouses")
    items = relationship("InventoryItem", back_populates="warehouse")


class InventoryItem(Base):
    """A serialized (or bulk) asset. Hot fields are columns; vendor long-tail
    lives in `attributes`, validated against device_type.attribute_schema."""
    __tablename__ = "inventory_item"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    serial_number = Column(String, nullable=True)
    mac_address = Column(String, nullable=True)
    # Cycle 5 Phase 1 (canon C13/F1.2, revision nc1a): device-identity half that
    # pairs with serial_number for the acs_device_registration match. Nullable
    # — populated by UI/import tooling, no automated backfill from attributes.
    oui = Column(String, nullable=True)
    status = Column(
        Enum(InventoryItemStatus), nullable=False,
        default=InventoryItemStatus.IN_STOCK, server_default='IN_STOCK'
    )
    condition = Column(
        Enum(InventoryItemCondition), nullable=False,
        default=InventoryItemCondition.NEW, server_default='NEW'
    )
    attributes = Column(JSON, nullable=True)
    purchase_date = Column(DateTime(timezone=True), nullable=True)
    warranty_until = Column(DateTime(timezone=True), nullable=True)
    cost = Column(Float, nullable=True)
    # Money-in-cents shadow column (Cycle 1 dual-write; Float `cost` drops in
    # Cycle 2). NULL stays NULL in the backfill — doc 16 §2.5.3.
    cost_cents = Column(BigInteger, nullable=True)
    notes = Column(String, nullable=True)
    # --- Cycle 7 management surface (doc 25 §2.3, revision nc2a_core_config) ---
    # How the CLI drivers reach a CORE-tier device. mgmt_port NULL -> driver
    # default (22 ssh / 23 telnet); cli_protocol is a CHECK-constrained string
    # (CLI_PROTOCOLS) selecting which driver the connectivity probe uses.
    mgmt_host = Column(String, nullable=True)
    mgmt_port = Column(Integer, nullable=True)
    cli_protocol = Column(String, nullable=True)
    # Stamped by the provisioning worker when a core_connectivity_check job
    # reaches a terminal state (ok = status SUCCEEDED). Read-only in the API.
    mgmt_last_check_at = Column(DateTime(timezone=True), nullable=True)
    mgmt_last_check_ok = Column(Boolean, nullable=True)

    # --- NAT transport (spec §8, revision nat1_gateway_transport) ---
    # The external port on the tenant's gateway that dst-nats to this device.
    # NEVER conflated with mgmt_port, which stays the device's REAL service
    # port (spec N1). NULL for every non-NAT device.
    nat_port = Column(Integer, nullable=True)
    # Pinned SSH host key (spec N9). Recorded on first successful connect;
    # any later mismatch is a hard failure, never an auto-add.
    mgmt_host_key = Column(String, nullable=True)

    # --- Figma redesign PR 8 (08-inventario §2.3) -----------------------------
    # One row per LOT for non-serialized device types (one per device_type +
    # location); always 1 for serialized gear. This is the whole non-serialized
    # story — no stock table, no movements table: equipment_event is already the
    # append-only ledger.
    quantity = Column(Integer, nullable=False, default=1, server_default='1')
    # Free-text display name for plant with no serial ("MUFA 1", "Router 563").
    label = Column(String(120), nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("device_type.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("warehouse.id", ondelete="SET NULL"), nullable=True
    )
    # Customer-premise assignment (CPE): who has this item installed.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="SET NULL"), nullable=True
    )
    client_service_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="SET NULL"), nullable=True
    )
    # "Con tecnico" custody (08-inventario §2.3). A vehicle warehouse is not a
    # person and cannot answer "empleado asignado"; EquipmentEvent.technician_id
    # is history, not current custody. SET NULL: an offboarded user leaves the
    # item in inventory, unassigned.
    custodian_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # --- network graph (doc 35 §2.1, revision ng1_network_graph) --------------
    # The company's plant is a tree of inventory items. `network_attached` marks
    # an item as part of that tree at all; a root is attached with no parent;
    # warehouse stock is simply not attached. Two flags rather than one because
    # `parent_id IS NULL` alone cannot distinguish "this is the core router"
    # from "this ONT is still in the van".
    #
    # RESTRICT on delete is deliberate: deleting an OLT must not silently
    # promote the 400 subscribers behind it to roots. Detach or re-parent the
    # children first — the API says so, and trg_inventory_item_detach_guard
    # enforces it.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="RESTRICT"),
        nullable=True, index=True,
    )
    network_attached = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The lp1 free-text labels (`parent_port`, `uplink_port` and the
    # uq_inventory_item_parent_port index) are unmapped since pt2 (doc 40
    # §4.2 C8a): the ports of an edge are network_link + inventory_item_port.
    # The DB keeps them until pt3_drop_port_labels (C8b).
    # mi2: where the item physically is (plant: MUFA / NAP geolocated by the
    # technician in the field). WGS84 degrees; precision in metres.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    gps_precision_m = Column(Float, nullable=True)

    company = relationship("Company", back_populates="inventory_items")
    device_type = relationship("DeviceType", back_populates="items")
    warehouse = relationship("Warehouse", back_populates="items")
    client = relationship("Client", back_populates="equipment")
    # No backref on User: "everything this technician holds" is a filtered
    # query, not a collection anyone loads off a user row.
    custodian = relationship("User", foreign_keys=[custodian_user_id])
    # Two FK paths now join inventory_item and client_service (this one, and
    # client_service.cpe_item_id pointing back) — both sides must name theirs.
    client_service = relationship(
        "ClientService", back_populates="equipment", foreign_keys=[client_service_id],
    )
    parent = relationship(
        "InventoryItem", remote_side=[id], back_populates="children",
    )
    children = relationship("InventoryItem", back_populates="parent")
    events = relationship(
        "EquipmentEvent", back_populates="item",
        cascade="all, delete-orphan", foreign_keys="EquipmentEvent.item_id",
    )
    # doc 40 §3.1. View-only: rows are written through the backend helpers
    # (sync_item_ports / set_uplink) and deleted by the DB cascades, never by
    # ORM collection bookkeeping.
    ports = relationship(
        "InventoryItemPort", viewonly=True,
        order_by="[InventoryItemPort.slot, InventoryItemPort.direction, InventoryItemPort.number]",
    )
    uplink = relationship(
        "NetworkLink", viewonly=True, uselist=False,
        primaryjoin="InventoryItem.id == foreign(NetworkLink.down_item_id)",
    )

    __table_args__ = (
        # doc 40 §3.1.1: target of the composite (id, company_id) FKs from
        # inventory_item_port and network_link, which make cross-tenant links
        # impossible. Always satisfiable: id is the PK.
        UniqueConstraint("id", "company_id", name="uq_inventory_item_id_company"),
        # Serial uniqueness per company (only when a serial is recorded).
        Index(
            "uq_inventory_item_company_serial",
            "company_id", "serial_number",
            unique=True,
            postgresql_where=text("serial_number IS NOT NULL"),
        ),
        Index("ix_inventory_item_company_status", "company_id", "status"),
        # Cycle 7 (doc 25 §2.3).
        CheckConstraint(_CLI_PROTOCOL_CHECK, name="ck_inventory_item_cli_protocol"),
        # spec §8 (nat1). nat_port uniqueness is per company because a tenant
        # has one gateway; two devices behind one external port would be a
        # config push to the wrong device.
        CheckConstraint(_NAT_PORT_CHECK, name="ck_inventory_item_nat_port"),
        CheckConstraint(_MGMT_PORT_CHECK, name="ck_inventory_item_mgmt_port"),
        Index(
            "uq_inventory_item_company_nat_port",
            "company_id", "nat_port",
            unique=True,
            postgresql_where=text("nat_port IS NOT NULL"),
        ),
        # Cycle 10 / doc 35 §2.1. Cycles, cross-tenant parents and the depth cap
        # are guarded by trg_inventory_item_graph_guard (ng1) — a CHECK cannot
        # express reachability. These two are the parts a CHECK *can* state.
        CheckConstraint(
            "parent_id IS NULL OR network_attached",
            name="ck_inventory_item_parent_attached",
        ),
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_inventory_item_not_self_parent",
        ),
        Index(
            "ix_inventory_item_company_attached", "company_id",
            postgresql_where=text("network_attached"),
        ),
        # Figma redesign PR 8 (08-inventario §2.3).
        CheckConstraint(
            _INVENTORY_QUANTITY_CHECK, name="ck_inventory_item_quantity_positive",
        ),
        # GET /inventory/summary groups on (company_id, device_type_id); the
        # "Con tecnico" tab and the technician app filter on the custodian.
        Index("ix_inventory_item_company_device_type", "company_id", "device_type_id"),
        Index("ix_inventory_item_company_custodian", "company_id", "custodian_user_id"),
    )


class InventoryItemPort(Base):
    """A physical port on one inventory item (doc 40 §3.1.1).

    Generated from device_type.port_template (origin TEMPLATE) or added per
    item (origin ITEM). An OLT slot is only the `slot` number (decision 3).
    The (id, item_id, company_id) unique is the target of network_link's
    composite FKs, so a link can never name another item's or tenant's port.
    """
    __tablename__ = "inventory_item_port"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name = Column(String(32), nullable=False)        # "1/4", "ether2", "OUT 6"
    slot = Column(SmallInteger, nullable=True)        # structural only
    number = Column(SmallInteger, nullable=False)
    medium = Column(String(8), nullable=False)
    direction = Column(String(4), nullable=False)
    origin = Column(String(8), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    item = relationship("InventoryItem", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "company_id"], ["inventory_item.id", "inventory_item.company_id"],
            name="fk_item_port_item", ondelete="CASCADE",
        ),
        UniqueConstraint("id", "item_id", "company_id", name="uq_item_port_identity"),
        CheckConstraint("slot BETWEEN 0 AND 255", name="ck_item_port_slot"),
        CheckConstraint("number BETWEEN 0 AND 4095", name="ck_item_port_number"),
        CheckConstraint("medium IN ('ETH','PON')", name="ck_item_port_medium"),
        CheckConstraint("direction IN ('UP','DOWN','ANY')", name="ck_item_port_direction"),
        CheckConstraint("origin IN ('TEMPLATE','ITEM')", name="ck_item_port_origin"),
        # Names are unique per item, case-insensitively.
        Index("uq_item_port_name", "item_id", text("lower(name)"), unique=True),
        # PON numbers feed the ONU-id arithmetic, so they are unique per
        # (slot, number, direction) too. Partial on both dialects so SQLite
        # test schemas match Postgres.
        Index(
            "uq_item_port_pon_number",
            "item_id", text("coalesce(slot, -1)"), "number", "direction",
            unique=True,
            postgresql_where=text("medium = 'PON'"),
            sqlite_where=text("medium = 'PON'"),
        ),
        Index("ix_item_port_company", "company_id"),
    )


class NetworkLink(Base):
    """A device's one upstream link, port to port (doc 40 §3.1.1).

    The invariant inventory_item[down_item_id].parent_id = up_item_id is
    written by one backend helper and backed by two deferred constraint
    triggers that live only in revision pt1_port_topology (never in this
    metadata: SQLite create_all cannot parse plpgsql). A device with a parent
    but no link is an "unported edge".

    The FKs to the ports are NO ACTION, not CASCADE: deleting only a device's
    own port must not silently remove its link. up_item_id has no FK of its
    own; the composite up-port FK pins it to the port's item.
    """
    __tablename__ = "network_link"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    up_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    up_port_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    down_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    down_port_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source = Column(String(8), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    up_port = relationship(
        "InventoryItemPort", viewonly=True,
        primaryjoin="foreign(NetworkLink.up_port_id) == InventoryItemPort.id",
    )
    down_port = relationship(
        "InventoryItemPort", viewonly=True,
        primaryjoin="foreign(NetworkLink.down_port_id) == InventoryItemPort.id",
    )
    down_item = relationship(
        "InventoryItem", viewonly=True,
        primaryjoin="foreign(NetworkLink.down_item_id) == InventoryItem.id",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["up_port_id", "up_item_id", "company_id"],
            ["inventory_item_port.id", "inventory_item_port.item_id",
             "inventory_item_port.company_id"],
            name="fk_link_up_port",
        ),
        ForeignKeyConstraint(
            ["down_item_id", "company_id"], ["inventory_item.id", "inventory_item.company_id"],
            name="fk_link_down_item", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["down_port_id", "down_item_id", "company_id"],
            ["inventory_item_port.id", "inventory_item_port.item_id",
             "inventory_item_port.company_id"],
            name="fk_link_down_port",
        ),
        UniqueConstraint("up_port_id", name="uq_link_up_port"),
        UniqueConstraint("down_port_id", name="uq_link_down_port"),
        UniqueConstraint("down_item_id", name="uq_link_down_item"),  # still a tree
        CheckConstraint("up_item_id <> down_item_id", name="ck_link_not_self"),
        CheckConstraint("source IN ('OFFICE','FIELD','IMPORT')", name="ck_link_source"),
        Index("ix_network_link_up_item", "up_item_id"),
        Index("ix_network_link_company", "company_id"),
    )


class EquipmentEvent(Base):
    """Append-only lifecycle ledger: replacement history, installs, transfers,
    maintenance — with technician attribution."""
    __tablename__ = "equipment_event"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    event_type = Column(Enum(EquipmentEventType), nullable=False)
    notes = Column(String, nullable=True)
    event_metadata = Column(JSON, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Replacement pairing: the item that replaced / was replaced by this one.
    related_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True
    )
    from_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("warehouse.id", ondelete="SET NULL"), nullable=True
    )
    to_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("warehouse.id", ondelete="SET NULL"), nullable=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="SET NULL"), nullable=True
    )
    client_service_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="SET NULL"), nullable=True
    )
    technician_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    item = relationship("InventoryItem", back_populates="events", foreign_keys=[item_id])
    related_item = relationship("InventoryItem", foreign_keys=[related_item_id])
    technician = relationship("User", foreign_keys=[technician_id])


# ---------------------------------------------------------------------------
# Provisioning automation (ADR-005/006)
# ---------------------------------------------------------------------------

class Playbook(Base):
    """Declarative, versioned provisioning recipe uploaded by the company.
    `definition` holds: variables (typed schema), steps (driver, template,
    validation, timeout), rollback steps. Validated on upload."""
    __tablename__ = "playbook"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    definition = Column(JSON, nullable=False)
    # Cycle 5 Phase 1 (canon C7, revision nc1a): stamped with `version` when a
    # dry-run ProvisioningJob (dry_run=true) for that version SUCCEEDS. A live
    # job is accepted iff last_dry_run_version == version, else 409
    # DRY_RUN_REQUIRED. The render-only preview endpoint does NOT satisfy this
    # gate; seeded system playbooks are exempt (SaaS-verified in CI).
    last_dry_run_version = Column(Integer, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    company = relationship("Company", back_populates="playbooks")
    creator = relationship("User", foreign_keys=[created_by])
    jobs = relationship("ProvisioningJob", back_populates="playbook")


# ---------------------------------------------------------------------------
# Playbook binding (Cycle 10, doc 35 §2.4, revision ng1_network_graph)
#
# A playbook runs on exactly ONE device, so it binds to the equipment rather
# than to a path: an OLT is configured the same way regardless of whose traffic
# crosses it. Resolution per node per purpose is:
#
#     node override  ->  device-type default  ->  none
#
# Binding lives in its own table rather than as a column on `playbook` so one
# playbook can serve several device types (one "MikroTik core config" for two
# router models) — which the retired `playbook.topology_id` ownership model
# made impossible.
# ---------------------------------------------------------------------------

class DeviceTypePlaybook(Base):
    """The type-level default playbook for a purpose."""
    __tablename__ = "device_type_playbook"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt,
                        onupdate=now_gt)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("device_type.id", ondelete="RESTRICT"), nullable=False
    )
    purpose = Column(String(50), nullable=False)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("playbook.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    device_type = relationship("DeviceType")
    playbook = relationship("Playbook")

    # The purpose-format CHECK (`purpose ~ '^[A-Z][A-Z0-9_]{0,49}$'`) is applied
    # in ng1 only, never in metadata — SQLite's create_all cannot parse `~`, and
    # the test suite builds its schema that way. Precedent:
    # ck_topology_playbook_purpose_format.
    __table_args__ = (
        UniqueConstraint("device_type_id", "purpose",
                         name="uq_device_type_playbook_purpose"),
    )


class InventoryItemPlaybook(Base):
    """A single node's override of its device type's default."""
    __tablename__ = "inventory_item_playbook"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt,
                        onupdate=now_gt)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="CASCADE"), nullable=False
    )
    purpose = Column(String(50), nullable=False)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("playbook.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    inventory_item = relationship("InventoryItem")
    playbook = relationship("Playbook")

    __table_args__ = (
        UniqueConstraint("inventory_item_id", "purpose", name="uq_item_playbook_purpose"),
    )


class ProvisioningRun(Base):
    """One service-path provisioning run: several devices, several playbooks.

    Cycle 10 (doc 35 §5). A run used to be a single ProvisioningJob executing
    one topology-wide playbook whose steps carried `target_position`. With
    playbooks bound per device type, a run spans SEVERAL playbooks — and a
    single job cannot honestly represent that: `playbook_id` is a single NOT
    NULL FK, and `uq_provisioning_job_device_lock` is keyed per device, so one
    job touching three devices could only ever hold one of the three locks.

    So the run is the container and each configured device gets its own child
    job. Children are created LAZILY, one at a time, in `plan` order: at most
    one child of a run is QUEUED or RUNNING at once. That needs no new
    ProvisioningJobStatus value (a "BLOCKED" state would touch every status
    consumer in three services) and no change to the worker's claim query.

    Three things this buys that the old single-job chain could not:
      - the per-device lock is finally correct — each child locks exactly the
        device it configures
      - retry and cancel become per-device
      - PENDING_INFORM applies to the CPE child alone instead of stalling the
        whole path
    """
    __tablename__ = "provisioning_run"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt,
                        onupdate=now_gt)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    purpose = Column(String(50), nullable=False)
    dry_run = Column(Boolean, nullable=False, default=False, server_default='false')
    status = Column(
        Enum(ProvisioningJobStatus, name="provisioningjobstatus"), nullable=False,
        default=ProvisioningJobStatus.QUEUED, server_default='QUEUED',
    )
    # The whole resolved path INCLUDING passive nodes, snapshotted at creation:
    # the run detail view must show what the path was when it ran, not what it
    # is now. [{position, item_id, serial, device_type_name, category_key,
    #           category_tier, is_passive, playbook_id, playbook_source,
    #           label, path_role, out_slot, out_port, out_port_name,
    #           playbook_version}]  (the last six since doc 40)
    path = Column(JSON, nullable=False)
    # The ordered subset that will actually be configured, leaf -> root:
    # [{item_id, playbook_id, playbook_version, category_key}]
    plan = Column(JSON, nullable=False)
    # {"shared": {...}, "device": {item_id: {...}}} — resolved ONCE at run
    # creation. advance_run builds later children from this rather than
    # re-resolving, so a re-parent landing mid-run cannot silently redirect the
    # remaining steps to a different set of devices than the ones the operator
    # saw and approved.
    frames = Column(JSON, nullable=False, default=dict)
    idempotency_key = Column(String, nullable=True)
    triggered_by = Column(
        Enum(ProvisioningTrigger), nullable=False,
        default=ProvisioningTrigger.USER, server_default='USER',
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    client_service = relationship("ClientService")
    jobs = relationship(
        "ProvisioningJob", back_populates="run", order_by="ProvisioningJob.run_position",
    )

    __table_args__ = (
        # Mirrors uq_provisioning_job_company_idem: a re-fire while a run is
        # still in flight dedupes instead of opening a second one.
        Index(
            "uq_provisioning_run_company_idem",
            "company_id", "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','RUNNING','PENDING_INFORM')"
            ),
        ),
        Index("ix_provisioning_run_service", "client_service_id", "created_at"),
        # ng2: the company-wide run list (PR 9) is company_id = ? ORDER BY created_at.
        Index("ix_provisioning_run_company_created", "company_id", "created_at"),
    )


class ProvisioningJob(Base):
    """Durable execution queue row. Claimed by the provisioning worker via
    SELECT ... FOR UPDATE SKIP LOCKED; retried with backoff up to max_attempts."""
    __tablename__ = "provisioning_job"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    status = Column(
        Enum(ProvisioningJobStatus), nullable=False,
        default=ProvisioningJobStatus.QUEUED, server_default='QUEUED'
    )
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    idempotency_key = Column(String, nullable=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    variables = Column(JSON, nullable=True)      # resolved playbook inputs
    log = Column(JSON, nullable=True)            # per-step structured results
    error = Column(String, nullable=True)
    triggered_by = Column(
        Enum(ProvisioningTrigger), nullable=False,
        default=ProvisioningTrigger.USER, server_default='USER'
    )
    # --- Cycle 5 Phase 1 (network config, revision nc1a) ---
    # canon C7: dry-run jobs never touch a device; a SUCCEEDED dry-run stamps
    # playbook.last_dry_run_version.
    dry_run = Column(Boolean, nullable=False, default=False, server_default='false')
    # canon C2: the parked step to settle when the inform arrives (PENDING_INFORM).
    pending_step_index = Column(Integer, nullable=True)
    # GenieACS NBI task ids being polled on the 202 / connection-request path.
    pending_task_ids = Column(JSON, nullable=True)
    # canon C11: the worker stamps this while RUNNING; the lease reaper re-queues
    # stale RUNNING jobs (Railway redeploys the worker on every merge). The
    # reaper does NOT increment attempts (the claim path already does).
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    # canon C11: per-device serialization key. The DB partial unique index below
    # is the serialization authority; in-process locks are a local optimization.
    device_lock_key = Column(String, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("playbook.id", ondelete="RESTRICT"), nullable=False
    )
    # Optional targets (what this job provisions).
    client_service_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="SET NULL"), nullable=True, index=True
    )
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True
    )
    # HTTP-driver credentials/endpoint source.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("integration.id", ondelete="SET NULL"), nullable=True
    )
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # Cycle 10 (doc 35 §5). NULL for every job that is not part of a service
    # path run — explicit-playbook jobs, ACS reboot/factory-reset, core
    # connectivity probes. Those are unchanged and must stay standalone.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provisioning_run.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    # 0-based index into ProvisioningRun.plan, i.e. leaf -> root order.
    run_position = Column(Integer, nullable=True)

    company = relationship("Company", back_populates="provisioning_jobs")
    run = relationship("ProvisioningRun", back_populates="jobs")
    playbook = relationship("Playbook", back_populates="jobs")
    client_service = relationship("ClientService")
    inventory_item = relationship("InventoryItem")
    integration = relationship("Integration")
    triggered_by_user = relationship("User", foreign_keys=[triggered_by_user_id])

    __table_args__ = (
        # Worker claim scan: QUEUED ordered by created_at.
        Index(
            "ix_provisioning_job_claim",
            "status", "scheduled_for", "created_at",
            postgresql_where=text("status = 'QUEUED'"),
        ),
        # Duplicate-enqueue guard (e.g. workflow retriggers). Cycle 5 Phase 1
        # (canon C2): the in-flight set now includes PENDING_INFORM so a parked
        # job still de-duplicates re-enqueues. The predicate is dropped and
        # recreated in revision nc1a (autogenerate cannot alter a partial index).
        Index(
            "uq_provisioning_job_company_idem",
            "company_id", "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN ('QUEUED','RUNNING','PENDING_INFORM')"
            ),
        ),
        # Cycle 5 Phase 1 (canon C11, revision nc1a): per-device serialization
        # authority — at most one live job per device_lock_key across the
        # in-flight set (QUEUED/RUNNING/PENDING_INFORM).
        Index(
            "uq_provisioning_job_device_lock",
            "device_lock_key",
            unique=True,
            postgresql_where=text(
                "device_lock_key IS NOT NULL AND status IN ('QUEUED','RUNNING','PENDING_INFORM')"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Network configuration (Cycle 5 Phase 1: TR-069 / GenieACS). Plan:
# docs/isp-platform/23-network-config-implementation-plan.md §2. Envelope-
# encrypted device secrets (device_credential, canon C1/C19), serial/OUI ->
# tenant mapping (acs_device_registration, canon C13), the tenant enable gate
# AND per-tenant transport/ACS config (provisioning_settings, canon C6 + C9 —
# tr1_transport_axis folded the old multi-row network_access table into it),
# and the append-only device audit trail
# (device_action_log, canon C14 — append-only enforced by a Postgres trigger
# created in revision nc1b, not here).
# ---------------------------------------------------------------------------

class DeviceCredential(Base):
    """Envelope-encrypted per-tenant device secret (canon C1/C19). AES-256-GCM
    with a per-row DEK wrapped by a KEK held in Railway env vars
    (database_utils.utils.crypto). `kind` is a CHECK-constrained string (open
    set, CREDENTIAL_KINDS) — adding a kind is a plain ALTER of the CHECK, never
    an ALTER TYPE. Secrets never round-trip: the Out schema exposes only
    has_secret + fingerprint (last 4).

    Binding FKs live ON this row (canon C19): resolution order at execution is
    inventory_item > device_type > UNBOUND (both FKs NULL = the company default;
    tr1_transport_axis removed the third FK, network_access_id, along with the
    table it pointed at). The tenant's TR-069 Inform credential is the one
    exception to "no other table carries an FK pointing at a credential":
    provisioning_settings.cwmp_credential_id / cwmp_pending_credential_id name
    it explicitly, because there is exactly one per tenant and the accept-both
    rotation window needs the pair to be stated rather than inferred."""
    __tablename__ = "device_credential"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)             # CHECK: CREDENTIAL_KINDS
    username = Column(String, nullable=True)          # display-safe, NOT secret
    # --- envelope encryption (all opaque to SQL; canon C1 column names) ---
    secret_ciphertext = Column(LargeBinary, nullable=False)  # 12-byte nonce prefixed
    dek_wrapped = Column(LargeBinary, nullable=False)        # AES-256-GCM(KEK, DEK)
    kek_id = Column(String, nullable=False)                  # key id into CREDENTIALS_KEKS
    # last 4 chars of a SHA-256 over the plaintext, computed at write time —
    # display-safe, lets the UI confirm which secret is stored without exposing it.
    fingerprint = Column(String, nullable=True)
    last_rotated_at = Column(DateTime(timezone=True), nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # --- bindings (canon C19: binding FKs live ON the credential row) ---
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_type.id", ondelete="SET NULL"), nullable=True, index=True
    )

    company = relationship("Company", back_populates="device_credentials")
    inventory_item = relationship("InventoryItem")
    device_type = relationship("DeviceType")

    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_device_credential_company_name"),
        CheckConstraint(_CREDENTIAL_KIND_CHECK, name="ck_device_credential_kind"),
    )

    @property
    def has_secret(self) -> bool:
        """Out-schema surface (canon C19): a credential always stores a secret,
        but expose the boolean explicitly so the API never implies the
        ciphertext could be read back."""
        return self.secret_ciphertext is not None


class AcsDeviceRegistration(Base):
    """Serial/OUI -> tenant mapping (canon C13): the tenant-stamping keystone.
    At first inform the GenieACS provision script calls back into Uplink; we
    look up the announcing device here and stamp the tag `t-{company_id}`.

    `company_id` is NULLABLE (NULL = QUARANTINED: informed without
    pre-registration, awaiting superadmin assignment). The (oui, serial_number)
    unique is GLOBAL (no company_id) so two tenants can never claim the same
    physical CPE — cross-tenant duplicate pre-registration is a 409 in the
    router. Status is DERIVED (no enum column), see `state`."""
    __tablename__ = "acs_device_registration"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=True, index=True
    )
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True
    )
    serial_number = Column(String, nullable=False)
    oui = Column(String, nullable=True)
    first_inform_at = Column(DateTime(timezone=True), nullable=True)
    last_inform_at = Column(DateTime(timezone=True), nullable=True)
    genieacs_device_id = Column(String, nullable=True)  # "OUI-ProductClass-Serial"
    # per-device CWMP connection-request credentials (canon C13): issued at
    # bootstrap, used by GenieACS connection requests — never blank/blank.
    # Envelope-encrypted via crypto.py (AAD = f"{company_id}:{registration_id}").
    cwmp_cr_username = Column(String, nullable=True)
    cwmp_cr_secret_ciphertext = Column(LargeBinary, nullable=True)
    cwmp_cr_dek_wrapped = Column(LargeBinary, nullable=True)
    cwmp_cr_kek_id = Column(String, nullable=True)
    # fg1: human author of a pre-registration (single/bulk). NULL for the
    # bootstrap/quarantine path and legacy rows. SET NULL keeps the row.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    company = relationship("Company", back_populates="acs_device_registrations")
    inventory_item = relationship("InventoryItem")
    created_by = relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        UniqueConstraint("oui", "serial_number", name="uq_acs_registration_identity"),
        # The UNIQUE above does NOT constrain rows whose oui is NULL (Postgres
        # treats NULLs as distinct), and `oui` IS nullable — _normalize_oui
        # (schemas/acs_registration.py) returns None unchanged for an omitted
        # OUI, so NULL-oui rows are ordinary API output. Without this index two
        # tenants can both pre-register the same serial: the router's 409 check
        # is check-then-insert with no DB backstop, and once Capa 3 ships the
        # inform-auth lookup's .first() would hand one tenant's CWMP password
        # to the other's CPE. sqlite_where mirrors postgresql_where, the
        # tr1's partial-index precedent (sqlite_where mirroring
        # postgresql_where).
        Index(
            "uq_acs_registration_serial_no_oui",
            "serial_number",
            unique=True,
            postgresql_where=text("oui IS NULL"),
            sqlite_where=text("oui IS NULL"),
        ),
    )

    @property
    def state(self) -> str:
        """Derived status (canon C13) — no enum column, matching the
        ServiceSuspension.reactivated_at NULL-episode pattern. Order matters:
        a quarantined device may already have informed, so company_id wins."""
        if self.company_id is None:
            return "QUARANTINED"
        if self.first_inform_at is None:
            return "PRE_REGISTERED"
        if self.last_inform_at is None:
            return "STALE"
        age = (now_gt() - make_aware_gt(self.last_inform_at)).total_seconds()
        return "ONLINE" if age <= ACS_STALE_AFTER_SECONDS else "STALE"


class ProvisioningSettings(Base):
    """Per-tenant provisioning, transport and ACS configuration — a singleton per
    tenant (canon C6 + C9). Absence of a row means DISABLED (fail-safe) and the
    row is created lazily / by tenant-onboarding automation, never seeded. Not a
    column on `company`: auth-erp owns that table and this is ISP-module config.

    tr1_transport_axis folded the whole `network_access` table in here. That table
    was multi-row only to serve a per-CIDR longest-prefix resolver
    (`mgmt_subnets`) that was never implemented and is now abandoned, and its
    `kind` discriminator conflated a settings bucket (`acs`) with a transport
    (`outbound`). Both questions are tenant-wide and mutually exclusive, which is
    what a singleton is for."""
    __tablename__ = "provisioning_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # master switch, default OFF (fail-safe).
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    # tenant default inform interval (seconds) pushed to CPE presets; NULL = use
    # the platform default.
    default_inform_interval = Column(Integer, nullable=True)

    # --- the transport axis (tr1_transport_axis) ---------------------------
    # Whose address the worker dials: the DEVICE's own mgmt_host, or the tenant
    # gateway that dst-nats to it. Replaces half of the old `mode` enum.
    dial_target = Column(
        String, nullable=False, default="device", server_default="device"
    )
    # Whether there is a hop in front of that address, and of what sort. The
    # other half of the old `mode`. 'agent' (canon C10's edge relay) is the
    # planned third value and needs no new column.
    proxy_kind = Column(String, nullable=False, default="none", server_default="none")
    # The SOCKS5 listener as "host:port"; REQUIRED when proxy_kind='socks5'
    # (ck_provisioning_settings_proxy_address). The hub technology is NOT
    # recorded and is none of the resolver's business — a Railway-internal
    # Pylon/ZeroTier proxy and an external WireGuard-hub VPS are the same thing
    # here.
    #
    # SECURITY PREREQUISITE, not code: an external value (a VPS running
    # microsocks, which ships with no authentication) MUST be firewalled to
    # Railway's egress, or anyone who learns the address gets a route into the
    # tenant LAN. See docs/network-models.md.
    proxy_address = Column(String, nullable=True)
    # The tenant gateway's address on the path WE dial; REQUIRED when
    # dial_target='gateway' (ck_provisioning_settings_gateway_host). Deliberately
    # String, not INET: a ZeroTier value is RFC1918 and a public value may be a
    # DDNS hostname, so no "globally routable" assertion is possible or wanted.
    # The per-device external port is inventory_item.nat_port, unchanged.
    gateway_host = Column(String, nullable=True)

    # --- ACS config, moved off network_access (tr1_transport_axis) ----------
    # Informational ONLY and read-only in the UI: nothing in code reads it. Its
    # job is telling an installer what to type into a CPE. It is deliberately
    # absent from ProvisioningSettingsUpdate so no write path can set it — an
    # `http://` value here would turn every CWMP POST into a bodyless GET at
    # Railway's edge, and an orphaned CPE has no remote fix.
    acs_base_url = Column(String, nullable=True)
    # ac1 (Capa 3, decision 8): do this tenant's CPEs have to prove a shared
    # secret at CWMP Inform? OFF by default, and off means ALLOW — a tenant that
    # never enrols behaves exactly as before, and so does a serial with no
    # acs_device_registration row. Both are required or auto-discovery and
    # quarantine break.
    acs_auth_required = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- the tenant TR-069 credential (tr1_transport_axis) ------------------
    # decision 12's accept-both rotation window, made EXPLICIT. It used to be
    # inferred as "newest vs second-newest HTTP_BASIC device_credential row bound
    # to the tenant's acs network_access row", which was fragile in both
    # directions: a third row was undefined, and the pair depended on a
    # `created_at DESC, id DESC` tie-break. One credential per tenant is now true
    # by construction.
    cwmp_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_credential.id", ondelete="SET NULL"), nullable=True
    )
    cwmp_pending_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_credential.id", ondelete="SET NULL"), nullable=True
    )

    company = relationship("Company", back_populates="provisioning_settings")
    cwmp_credential = relationship(
        "DeviceCredential", foreign_keys=[cwmp_credential_id]
    )
    cwmp_pending_credential = relationship(
        "DeviceCredential", foreign_keys=[cwmp_pending_credential_id]
    )

    __table_args__ = (
        CheckConstraint(
            _PROVISIONING_DIAL_TARGET_CHECK,
            name="ck_provisioning_settings_dial_target",
        ),
        CheckConstraint(
            _PROVISIONING_PROXY_KIND_CHECK, name="ck_provisioning_settings_proxy_kind"
        ),
        CheckConstraint(
            _PROVISIONING_PROXY_ADDRESS_CHECK,
            name="ck_provisioning_settings_proxy_address",
        ),
        CheckConstraint(
            _PROVISIONING_GATEWAY_HOST_CHECK,
            name="ck_provisioning_settings_gateway_host",
        ),
        CheckConstraint(
            _PROVISIONING_CWMP_PAIR_CHECK, name="ck_provisioning_settings_cwmp_pair"
        ),
    )


class DeviceActionLog(Base):
    """Append-only device audit trail (canon C14). Distinct from auth's
    AuditLog (super-admin actions) and EquipmentEvent (stock movements). No
    `updated_at` — rows are immutable. Append-only is enforced AT THE DATABASE
    by a BEFORE UPDATE OR DELETE trigger raising an exception, created in the
    hand-written revision nc1b (not here). The app layer exposes read-only
    list/get; writes happen exclusively in the worker/backend service modules."""
    __tablename__ = "device_action_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    actor_kind = Column(String, nullable=False)          # user | automation | system
    device_kind = Column(String, nullable=True)          # cpe | olt | ...
    device_identity = Column(String, nullable=True)      # serial or host
    action = Column(String, nullable=False)              # 'ont.add', 'cpe.factory_reset', ...
    before_data = Column(JSON, nullable=True)            # secret-redacted before insert
    after_data = Column(JSON, nullable=True)
    provisioning_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provisioning_job.id", ondelete="SET NULL"), nullable=True
    )
    detail = Column(JSON, nullable=True)

    company = relationship("Company", back_populates="device_action_logs")
    actor = relationship("User", foreign_keys=[actor_user_id])
    provisioning_job = relationship("ProvisioningJob")

    __table_args__ = (
        Index("ix_device_action_log_company_created", "company_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Insights (Cycle 4, v2 since iv1_insights_v2): tenant-defined dashboards of
# charts over existing entities. `spec` is an OPAQUE query-spec v2 JSON
# ({"version": 2, "entity", "measures", "dimensions", "time", "filters",
# "order", "limit"}) owned, validated and normalized by backend-erp's
# insights/spec.py; models-utils never parses it. `viz` and
# `default_time_range` are opaque JSON validated by backend-erp the same way.
# Available to every tenant (no tier module gate).
# ---------------------------------------------------------------------------

class InsightDashboard(Base):
    """A named collection of charts (insight_chart), scoped to a company."""
    __tablename__ = "insight_dashboard"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    ordering = Column(Integer, nullable=False, default=0, server_default='0')
    # TimeRange JSON: {"preset": "..."} or {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"};
    # NULL = no dashboard default. Validated by backend-erp on write.
    default_time_range = Column(JSON, nullable=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )

    company = relationship("Company", back_populates="insight_dashboards")
    charts = relationship(
        "InsightChart", back_populates="dashboard",
        cascade="all, delete-orphan", order_by="InsightChart.ordering",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_insight_dashboard_company_name"),
    )


class InsightChart(Base):
    """One chart within a dashboard. `spec` is an opaque query-spec v2 JSON
    compiled server-side by backend-erp — no company_id here, tenant scope
    derives via dashboard_id (scoping-through-parent)."""
    __tablename__ = "insight_chart"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    title = Column(String, nullable=False)
    chart_type = Column(Enum(InsightChartType), nullable=False)
    # Opaque QuerySpec v2 JSON, owned and validated by backend-erp
    # (insights/spec.py); stored as its normalized dump.
    spec = Column(JSON, nullable=False)
    # Viz JSON: {"width": 1|2|3, "stacked": bool}; NULL = defaults.
    # Validated by backend-erp on write.
    viz = Column(JSON, nullable=True)
    ordering = Column(Integer, nullable=False, default=0, server_default='0')

    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("insight_dashboard.id", ondelete="CASCADE"), nullable=False, index=True
    )

    dashboard = relationship("InsightDashboard", back_populates="charts")
