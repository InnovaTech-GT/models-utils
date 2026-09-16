# schemas/client.py
from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Literal, Optional, List
from datetime import datetime
from uuid import UUID

from database_utils.models.crm import RecurrenceEnum, ServiceAvailability
from database_utils.models.isp import ClientServiceStatus, ServicePlanType

from .user import UserOut


# --- Figma redesign PR 3 (03-clientes §2.3): the "Cuenta" rollup. ---
# COMPUTED by backend-erp from Order + Payment, never stored: a stored
# balance would be a second source of truth that goes stale the moment a
# payment lands. Money is integer cents, like every other amount.
class ClientAccountOut(BaseModel):
    state: Literal["CURRENT", "OVERDUE", "SUSPENSION_RISK"]
    overdue_cents: int = 0
    # ACTIVE orders with payment_status PENDING|PARTIAL.
    pending_orders: int = 0
    # "Desde:" — the oldest unpaid due date.
    oldest_due_date: Optional[datetime] = None


class ClientServiceSummaryOut(BaseModel):
    """The "Servicios" cell of the clients table — one chip per non-cancelled
    service. Deliberately NOT ClientServiceOut: the list renders a name, a
    type and a status, and shipping the full service (plan, provisioning
    state, billing) on every row of a 100-row page is pure payload."""
    id: UUID
    plan_name: str
    plan_type: ServicePlanType
    status: ClientServiceStatus

    model_config = ConfigDict(from_attributes=True)


class ClientServiceBillingOut(BaseModel):
    """Per-service billing line of GET /clients/{id}/account. charge_cents is
    computed with the established `price_cents ?? to_cents(price)` fallback
    (service_plan.price_cents is nullable) times the service quantity."""
    client_service_id: UUID
    plan_name: str
    recurrence: Optional[RecurrenceEnum] = None
    next_generation_date: Optional[datetime] = None
    charge_cents: int = 0


class ClientAccountDetailOut(ClientAccountOut):
    services: List[ClientServiceBillingOut] = []
    # --- Figma redesign PR 5 (05-pagos §3.2, master plan §2.2): PR 5 EXTENDS
    # this schema rather than declaring a second account shape. All COMPUTED
    # by backend-erp from Order + Payment + ClientService, never stored.
    # "A cobrar" — balance of ACTIVE orders with payment_status PENDING|PARTIAL
    # (a superset of overdue_cents, which only counts the past-due ones).
    receivable_cents: int = 0
    # "Saldo a favor". ALWAYS 0 and rendered as "Próximamente": there is no
    # credit ledger and PaymentService rejects overpayment by design. The field
    # exists so the card has a contract to read; do not compute it here without
    # the client_credit table (05-pagos §2).
    credit_cents: int = 0
    # "Cada N de cada mes" — day-of-month of ClientService.next_generation_date.
    next_payment_day: Optional[int] = None
    recurrence: Optional[RecurrenceEnum] = None
    # len(services) before any display truncation.
    services_total: int = 0


class ClientDeactivateIn(BaseModel):
    """POST /clients/{id}/deactivate. `cascade` opts into cancelling the
    client's still-active services in the same transaction; without it the
    endpoint 409s (CLIENT_HAS_ACTIVE_SERVICES) rather than silently leaving
    a deactivated client billing."""
    reason: Optional[str] = None
    cascade: bool = False


class ClientBase(BaseModel):
    name: str
    tax_id: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    email: Optional[EmailStr]
    contact: Optional[str]
    observations: Optional[str]
    # Guatemalan CUI (cl1). Nullable by decision: legacy rows have none and
    # the xlsx import may leave it empty. Uniqueness is enforced per company
    # by a partial unique index, not here.
    dpi: Optional[str] = None
    # ISP fields (ADR-004)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    gps_precision_m: Optional[float] = None
    service_availability: ServiceAvailability = ServiceAvailability.UNKNOWN


class ClientCreate(ClientBase):
    company_id: Optional[UUID] = None  # Optional - will be set from authenticated user context
    advisor_id: Optional[UUID] = None
    assigned_technician_id: Optional[UUID] = None
    custom_field_values: Optional[List["ClientCustomFieldValueInput"]] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    contact: Optional[str] = None
    observations: Optional[str] = None
    dpi: Optional[str] = None
    company_id: Optional[UUID] = None
    advisor_id: Optional[UUID] = None
    assigned_technician_id: Optional[UUID] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    gps_precision_m: Optional[float] = None
    service_availability: Optional[ServiceAvailability] = None
    custom_field_values: Optional[List["ClientCustomFieldValueInput"]] = None


class ClientOut(ClientBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    # cl1: NULL = active (Actuales tab); set = Histórico + "Fecha de baja".
    deactivated_at: Optional[datetime] = None
    deactivation_reason: Optional[str] = None
    # COMPUTED rollups, filled by backend-erp's list/detail endpoints only
    # (page-scoped grouped queries, no N+1). None/[] means "not computed",
    # not "no data" — same contract as services_total below.
    account: Optional[ClientAccountOut] = None
    services_summary: List[ClientServiceSummaryOut] = []
    # Services summary rollup (cf1, replaces the dropped stored
    # installation_status): read-only, COMPUTED by backend-erp's clients
    # list/detail endpoints from client_service rows
    # (install_state='INSTALLED' for the second count) — never stored,
    # never on Create/Update. Defaults keep the schema valid for callers
    # that hydrate straight from the ORM row.
    services_total: int = 0
    services_installed: int = 0
    advisor_id: Optional[UUID]
    advisor: Optional[UserOut] = None
    assigned_technician_id: Optional[UUID] = None
    assigned_technician: Optional[UserOut] = None
    custom_field_values: Optional[List["ClientCustomFieldValueOut"]] = None

    model_config = ConfigDict(from_attributes=True)


# Import at the end to avoid circular imports
from .custom_field import ClientCustomFieldValueInput, ClientCustomFieldValueOut

# Rebuild model to resolve forward references
ClientCreate.model_rebuild()
ClientUpdate.model_rebuild()
ClientOut.model_rebuild()
