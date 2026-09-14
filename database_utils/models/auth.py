from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Table, Text, JSON, Uuid
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database_utils.database import Base
from ..utils.timezone_utils import now_gt

from typing import Optional
import uuid

# Association table for many-to-many relationship between Role and Permission
role_permission = Table(
    'role_permission',
    Base.metadata,
    Column('role_id', Uuid, ForeignKey('role.id', ondelete='CASCADE'), primary_key=True),
    Column('permission_id', Uuid, ForeignKey('permission.id', ondelete='CASCADE'), primary_key=True)
)

# Association table for many-to-many relationship between User and Role
user_role = Table(
    'user_role',
    Base.metadata,
    Column('user_id', Uuid, ForeignKey('user.id', ondelete='CASCADE'), primary_key=True),
    Column('role_id', Uuid, ForeignKey('role.id', ondelete='CASCADE'), primary_key=True)
)

# Association table for many-to-many relationship between UserInvitation and Role
user_invitation_role = Table(
    'user_invitation_role',
    Base.metadata,
    Column('invitation_id', Uuid, ForeignKey('user_invitation.id', ondelete='CASCADE'), primary_key=True),
    Column('role_id', Uuid, ForeignKey('role.id', ondelete='CASCADE'), primary_key=True)
)

class Tier(Base):
    __tablename__ = "tier"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False, unique=True)

    # Billing fields
    price = Column(Float, nullable=False, default=0.0)  # Monthly price in GTQ (e.g. 299.00)
    price_yearly = Column(Float, nullable=True)  # Yearly price in GTQ (None = yearly not available)
    billing_cycle = Column(String, nullable=False, default="MONTHLY")  # MONTHLY, YEARLY (tier default)
    features = Column(JSON, nullable=True)  # {"max_users": 10, "max_products": 100, "support": "basic"}
    modules = Column(JSON, nullable=True)  # ["core", "admin", "management", "automations"]
    stripe_price_id = Column(String, nullable=True)  # Stripe Price ID for future integration
    # Recurrente integration (tenant-pays-Uplink checkout). NULL price id =
    # tier not purchasable online yet (frontend shows it as coming soon).
    recurrente_product_id = Column(String, nullable=True)
    recurrente_price_id = Column(String, nullable=True)          # monthly price
    recurrente_price_yearly_id = Column(String, nullable=True)   # yearly price
    is_active = Column(Boolean, default=True, nullable=False)  # Can be assigned to new companies

    # Relationships
    companies = relationship("Company", back_populates="tier")
    subscriptions = relationship("Subscription", back_populates="tier")


class Company(Base):
    __tablename__ = "company"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    start_date = Column(DateTime(timezone=True), nullable=True)
    tier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tier.id"), nullable=False)
    tax_id = Column(String, nullable=True)
    address = Column(String, nullable=True)
    # Recurrente customer id — created lazily on the company's first checkout.
    recurrente_customer_id = Column(String, nullable=True)

    # Relationships
    tier = relationship("Tier", back_populates="companies")
    users = relationship("User", back_populates="company", cascade="all, delete-orphan")
    clients = relationship("Client", back_populates="company", cascade="all, delete-orphan")
    products = relationship("Product", back_populates="company", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="company", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="company", cascade="all, delete-orphan")
    custom_field_definitions = relationship("CustomFieldDefinition", back_populates="company", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="company", cascade="all, delete-orphan")
    recurring_orders = relationship("RecurringOrder", back_populates="company", cascade="all, delete-orphan")
    subscription = relationship("Subscription", back_populates="company", uselist=False, cascade="all, delete-orphan")
    payment_methods = relationship("PaymentMethod", back_populates="company", cascade="all, delete-orphan")
    task_states = relationship("TaskState", back_populates="company", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="company", cascade="all, delete-orphan")
    # uplink-mobile integration (uf1/rs1)
    uploaded_files = relationship("UploadedFile", back_populates="company", cascade="all, delete-orphan")
    collection_routes = relationship("CollectionRoute", back_populates="company", cascade="all, delete-orphan")
    cash_sessions = relationship("CashSession", back_populates="company", cascade="all, delete-orphan")
    task_templates = relationship("TaskTemplate", back_populates="company", cascade="all, delete-orphan")
    workflows = relationship("Workflow", back_populates="company", cascade="all, delete-orphan")
    integrations = relationship("Integration", back_populates="company", cascade="all, delete-orphan")
    roles = relationship("Role", back_populates="company", cascade="all, delete-orphan")
    # ISP modules
    service_plans = relationship("ServicePlan", back_populates="company", cascade="all, delete-orphan")
    client_services = relationship("ClientService", back_populates="company", cascade="all, delete-orphan")
    device_types = relationship("DeviceType", back_populates="company", cascade="all, delete-orphan")
    warehouses = relationship("Warehouse", back_populates="company", cascade="all, delete-orphan")
    inventory_items = relationship("InventoryItem", back_populates="company", cascade="all, delete-orphan")
    # Cycle 2 D6: network_node_types/network_nodes rels removed with the graph
    # (revision c2d_graph_removal); topologies (D5) is the replacement.
    playbooks = relationship("Playbook", back_populates="company", cascade="all, delete-orphan")
    provisioning_jobs = relationship("ProvisioningJob", back_populates="company", cascade="all, delete-orphan")
    # Cycle 4: insights dashboards.
    insight_dashboards = relationship("InsightDashboard", back_populates="company", cascade="all, delete-orphan")
    # Cycle 5 Phase 1: network configuration (TR-069 / GenieACS).
    device_credentials = relationship("DeviceCredential", back_populates="company", cascade="all, delete-orphan")
    network_accesses = relationship("NetworkAccess", back_populates="company", cascade="all, delete-orphan")
    acs_device_registrations = relationship("AcsDeviceRegistration", back_populates="company", cascade="all, delete-orphan")
    provisioning_settings = relationship("ProvisioningSettings", back_populates="company", uselist=False, cascade="all, delete-orphan")
    device_action_logs = relationship("DeviceActionLog", back_populates="company", cascade="all, delete-orphan")


class Permission(Base):
    __tablename__ = "permission"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False, unique=True)  # e.g., "clients.read", "orders.create"
    resource = Column(String, nullable=False)  # e.g., "clients", "orders", "products"
    action = Column(String, nullable=False)  # e.g., "create", "read", "update", "delete"
    description = Column(Text, nullable=True)

    # Relationships
    roles = relationship("Role", secondary=role_permission, back_populates="permissions")


class Role(Base):
    __tablename__ = "role"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False)  # e.g., "ADMIN", "MANAGER", "SALES"; uniqueness enforced in app logic
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, default=False)  # System roles (ADMIN, USER) cannot be deleted
    # NULL = global base role (managed by superadmin); non-NULL = company-specific custom role
    company_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=True, index=True)

    # Relationships
    permissions = relationship("Permission", secondary=role_permission, back_populates="roles")
    users = relationship("User", secondary=user_role, back_populates="roles")
    company = relationship("Company", back_populates="roles")


class User(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    age = Column(Integer, nullable=False)
    password_hash = Column(String, nullable=False)
    active = Column(Boolean, default=True, nullable=False)  # User activation/deactivation
    email_verified = Column(Boolean, default=False, nullable=False)  # Gates login until confirmed

    company_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=True)
    is_super_admin = Column(Boolean, default=False, nullable=False)

    # Relationships
    company = relationship("Company", back_populates="users")
    clients = relationship(
        "Client", back_populates="advisor", cascade="all, delete-orphan",
        foreign_keys="Client.advisor_id",
    )
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    roles = relationship("Role", secondary=user_role, back_populates="users")


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    status = Column(String, nullable=False, default="PENDING")  # PENDING, ACCEPTED, REJECTED

    # --- [INIT] Possible User data: Add User Fields ---
    # Note: Notifications store user invitation data, not actual user records
    # For role assignments, use the UserInvitation model with its role relationship
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    age = Column(Integer, nullable=False)
    password_hash = Column(String, nullable=False)
    pending_role_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # --- [END] Possible User data: Add User Fields ---

    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=True)

    # Relationships
    user = relationship("User", back_populates="notifications")
    company = relationship("Company", back_populates="notifications")


class AuditLog(Base):
    """Audit log for tracking super admin actions"""
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    action = Column(String, nullable=False)  # e.g., "company.disable", "tier.create"
    resource_type = Column(String, nullable=False)  # e.g., "company", "tier", "user"
    resource_id = Column(String, nullable=True)  # Changed to String to store UUID as text
    details = Column(JSON, nullable=True)  # Store before/after state, additional context
    ip_address = Column(String, nullable=True)

    # Relationships
    user = relationship("User")


class UserInvitation(Base):
    """User invitation system for admin-initiated user enrollment"""
    __tablename__ = "user_invitation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # 7 days from creation
    email = Column(String, nullable=False)
    token = Column(String, nullable=False, unique=True)  # UUID for invitation link
    status = Column(String, nullable=False, default="PENDING")  # PENDING, ACCEPTED, EXPIRED, REVOKED
    name = Column(String, nullable=True)  # Optional pre-fill by admin

    # Foreign keys
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    company = relationship("Company")
    invited_by = relationship("User", foreign_keys=[invited_by_user_id])
    accepted_user = relationship("User", foreign_keys=[accepted_user_id])
    roles = relationship("Role", secondary=user_invitation_role)


class EmailVerificationToken(Base):
    """One-time token sent by email to confirm a newly-created account before it can log in."""
    __tablename__ = "email_verification_token"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    token = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)

    user = relationship("User")


class PasswordResetToken(Base):
    """One-time token sent by email to authorize a password change."""
    __tablename__ = "password_reset_token"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    token = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)

    user = relationship("User")


class Subscription(Base):
    """Subscription billing for companies"""
    __tablename__ = "subscription"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    # Subscription details
    status = Column(String, nullable=False, default="ACTIVE")  # ACTIVE, PAST_DUE, CANCELED, TRIALING
    billing_type = Column(String, nullable=False, default="AUTOMATIC")  # AUTOMATIC (Stripe), MANUAL (cash/wire)
    billing_cycle = Column(String, nullable=False, default="MONTHLY", server_default="MONTHLY")  # Company's chosen cycle: MONTHLY, YEARLY
    current_period_start = Column(DateTime(timezone=True), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end = Column(Boolean, default=False, nullable=False)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    trial_end = Column(DateTime(timezone=True), nullable=True)

    # Foreign keys
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, unique=True)
    tier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tier.id"), nullable=False)

    # Stripe integration
    stripe_subscription_id = Column(String, nullable=True, unique=True)
    stripe_customer_id = Column(String, nullable=True)

    # Recurrente integration — set by the subscription.create webhook.
    recurrente_subscription_id = Column(String, nullable=True, unique=True)
    recurrente_checkout_id = Column(String, nullable=True)
    card_last4 = Column(String, nullable=True)
    card_brand = Column(String, nullable=True)

    # Relationships
    company = relationship("Company", back_populates="subscription")
    tier = relationship("Tier", back_populates="subscriptions")
    invoices = relationship("BillingInvoice", back_populates="subscription", cascade="all, delete-orphan")


class PaymentMethod(Base):
    """Payment methods for company billing"""
    __tablename__ = "payment_method"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    # Payment method details
    type = Column(String, nullable=False)  # "card", "bank_account"
    last4 = Column(String, nullable=False)
    expiry_month = Column(Integer, nullable=True)
    expiry_year = Column(Integer, nullable=True)
    brand = Column(String, nullable=True)  # "visa", "mastercard", etc.
    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Foreign keys
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True)

    # Stripe integration
    stripe_payment_method_id = Column(String, nullable=True, unique=True)

    # Relationships
    company = relationship("Company", back_populates="payment_methods")


class BillingInvoice(Base):
    """Subscription billing invoices (different from CRM invoices for customer orders)"""
    __tablename__ = "billing_invoice"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    # Invoice details
    invoice_number = Column(String, nullable=False, unique=True)
    invoice_date = Column(DateTime(timezone=True), nullable=False)
    due_date = Column(DateTime(timezone=True), nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)

    # Amounts in cents
    subtotal = Column(Integer, nullable=False)
    tax = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False)

    # Status
    status = Column(String, nullable=False, default="PENDING")  # PENDING, PAID, FAILED, REFUNDED
    payment_type = Column(String, default="AUTOMATIC")  # AUTOMATIC, MANUAL

    # Manual payment tracking
    manual_payment_method = Column(String, nullable=True)  # "Wire Transfer", "Check", "Cash"
    manual_payment_note = Column(Text, nullable=True)
    marked_paid_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id"), nullable=True)

    # Foreign keys
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("subscription.id", ondelete="CASCADE"), nullable=False)
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("payment_method.id", ondelete="SET NULL"), nullable=True)

    # Stripe integration
    stripe_invoice_id = Column(String, nullable=True, unique=True)
    stripe_payment_intent_id = Column(String, nullable=True)

    # Recurrente integration — second idempotency layer for webhook-recorded
    # charges (one invoice per Recurrente payment intent).
    recurrente_intent_id = Column(String, nullable=True, unique=True)

    # Additional details
    billing_reason = Column(String, nullable=True)  # "subscription_cycle", "subscription_create", "manual"
    notes = Column(Text, nullable=True)

    # Relationships
    subscription = relationship("Subscription", back_populates="invoices")
    payment_method = relationship("PaymentMethod")
    marked_paid_by = relationship("User", foreign_keys=[marked_paid_by_user_id])


class BillingWebhookEvent(Base):
    """Idempotency log for Recurrente webhook deliveries — PK is the svix-id
    delivery header, so a redelivered event is a no-op (oficina precedent)."""
    __tablename__ = "billing_webhook_event"

    svix_id = Column(String, primary_key=True)
    event_type = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)


