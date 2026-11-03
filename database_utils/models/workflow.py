"""Workflows. Two engines share these tables (doc 63-workflow-engine §2.2):

- engine_version = 1: the legacy in-process engine (utils/workflow_engine.py),
  defined by workflow_trigger / workflow_step / workflow_step_edge rows.
- engine_version = 2: the durable engine (backend-erp `workflows/`), defined by
  versioned JSON in workflow_version.definition; its runs extend
  workflow_execution / workflow_step_execution. Revision wf1_workflow_engine_v2.
"""
from sqlalchemy import (
    Column, String, Boolean, JSON, DateTime, ForeignKey, Enum, Uuid, Float,
    BigInteger, Integer, SmallInteger, Text, Index, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database_utils.database import Base
from ..utils.timezone_utils import now_gt

import enum
import uuid


class TriggerEventType(str, enum.Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"


class StepActionType(str, enum.Enum):
    UPDATE_FIELD = "UPDATE_FIELD"
    CREATE_ENTITY = "CREATE_ENTITY"
    HTTP_REQUEST = "HTTP_REQUEST"
    ENQUEUE_PROVISIONING = "ENQUEUE_PROVISIONING"
    # Installation flow (doc 16 §5.2): DB enum values added by revision
    # c1e_install_actions (ALTER TYPE ... ADD VALUE, irreversible).
    CREATE_ORDER = "CREATE_ORDER"
    CREATE_TASK = "CREATE_TASK"


class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    # wf1 (ALTER TYPE ... ADD VALUE, irreversible): v2 waits/retries and cancels.
    WAITING = "WAITING"
    CANCELLED = "CANCELLED"


class WorkflowVersionStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"


WORKFLOW_TRIGGER_TYPES = ("event", "date", "bot")
_JSONB = JSON().with_variant(JSONB(), "postgresql")


class Workflow(Base):
    __tablename__ = "workflow"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- engine v2 (wf1) ---
    engine_version = Column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    # The PUBLISHED version runs start from. use_alter: workflow <-> version
    # is a DDL cycle; post_update on the relationship breaks the flush cycle.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_version.id", ondelete="SET NULL", use_alter=True,
                   name="fk_workflow_active_version"),
        nullable=True,
    )
    source_template_key = Column(String(64), nullable=True)
    # Template version of the ACTIVE definition (written by install/publish only).
    source_template_version = Column(Integer, nullable=True)
    template_params = Column(JSON, nullable=True)
    ignored_template_version = Column(Integer, nullable=True)

    # Relationships
    company = relationship("Company", back_populates="workflows")
    active_version = relationship(
        "WorkflowVersion", foreign_keys=[active_version_id], post_update=True,
    )
    versions = relationship(
        "WorkflowVersion", foreign_keys="WorkflowVersion.workflow_id", back_populates="workflow",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    triggers = relationship("WorkflowTrigger", back_populates="workflow", cascade="all, delete-orphan")
    steps = relationship("WorkflowStep", back_populates="workflow", cascade="all, delete-orphan")
    edges = relationship("WorkflowStepEdge", back_populates="workflow", cascade="all, delete-orphan")
    executions = relationship("WorkflowExecution", back_populates="workflow", cascade="all, delete-orphan")


class WorkflowTrigger(Base):
    __tablename__ = "workflow_trigger"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    resource_type = Column(String, nullable=False)  # "order", "client", "product", "task", etc.
    event_type = Column(Enum(TriggerEventType), nullable=False)
    field_conditions = Column(JSON, nullable=True)
    # Example: {"field": "status", "operator": "changed_to", "value": "CANCELLED"}

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False
    )

    # Relationships
    workflow = relationship("Workflow", back_populates="triggers")


class WorkflowStep(Base):
    __tablename__ = "workflow_step"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    action_type = Column(Enum(StepActionType), nullable=False)
    action_config = Column(JSON, nullable=False)
    # Example for UPDATE_FIELD:
    # {"resource_type": "order", "resource_id_source": "trigger", "updates": {"paid": true}}
    # Example for CREATE_ENTITY:
    # {"resource_type": "task", "data": {"name": "Follow up", "status": "PENDING"}}

    position_x = Column(Float, nullable=True, default=0)
    position_y = Column(Float, nullable=True, default=0)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False
    )

    # Relationships
    workflow = relationship("Workflow", back_populates="steps")
    # Cycle 2 (run-history durability, doc 18 automations-ux §2 Fix B):
    # deleting a step must NOT destroy its execution history — step_id is now
    # nullable with ON DELETE SET NULL at the DB level (revision
    # c2e_step_exec_snapshot). No ORM delete-orphan cascade here anymore;
    # passive_deletes=True tells SQLAlchemy to let the DB's SET NULL handle it
    # instead of emitting per-row UPDATE/DELETE statements on step deletion.
    step_executions = relationship(
        "WorkflowStepExecution", back_populates="step", passive_deletes=True,
    )


class WorkflowStepEdge(Base):
    __tablename__ = "workflow_step_edge"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)

    from_step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_step.id", ondelete="CASCADE"), nullable=False
    )
    to_step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_step.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False
    )

    # Relationships
    workflow = relationship("Workflow", back_populates="edges")
    from_step = relationship("WorkflowStep", foreign_keys=[from_step_id])
    to_step = relationship("WorkflowStep", foreign_keys=[to_step_id])


class WorkflowExecution(Base):
    __tablename__ = "workflow_execution"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Enum(ExecutionStatus), nullable=False, default=ExecutionStatus.PENDING)
    trigger_event = Column(JSON, nullable=False)
    # {"resource_type": "order", "event_type": "UPDATED", "resource_id": "uuid", "changes": {...}}
    error = Column(String, nullable=True)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False
    )

    # --- engine v2 (wf1). definition NULL = a legacy run. ---
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt,
                        server_default=text("now()"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # History/labels only: the executor runs the frozen `definition` copy.
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_version.id", ondelete="SET NULL"), nullable=True
    )
    definition = Column(JSON, nullable=True)
    # ev:{event_id} / task_done:{task_id} / date:… / bot:… / test:{uuid}
    dedupe_key = Column(String(160), nullable=True)
    depth = Column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    is_test = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    test_options = Column(JSON, nullable=True)      # {send_to_me, requested_by}
    context = Column(JSON, nullable=True)           # {trigger: payload, steps: {key: output}}
    cursor = Column(JSON, nullable=True)            # {queue: [keys], recoveries: int}
    resume_at = Column(DateTime(timezone=True), nullable=True)
    claim_token = Column(Uuid, nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    # The run's subject, for history filters. Not FKs.
    client_id = Column(Uuid, nullable=True, index=True)
    client_service_id = Column(Uuid, nullable=True, index=True)
    customer_message = Column(Text, nullable=True)  # C4 bot reply
    error_code = Column(String(48), nullable=True)

    __table_args__ = (
        Index("uq_workflow_execution_dedupe", "workflow_id", "dedupe_key", unique=True,
              postgresql_where=text("dedupe_key IS NOT NULL"),
              sqlite_where=text("dedupe_key IS NOT NULL")),
        Index("ix_workflow_execution_runnable", "resume_at",
              postgresql_where=text("status IN ('PENDING', 'WAITING') AND definition IS NOT NULL"),
              sqlite_where=text("status IN ('PENDING', 'WAITING') AND definition IS NOT NULL")),
    )

    # Relationships
    workflow = relationship("Workflow", back_populates="executions")
    version = relationship("WorkflowVersion")
    step_executions = relationship(
        "WorkflowStepExecution", back_populates="execution", cascade="all, delete-orphan"
    )


class WorkflowStepExecution(Base):
    __tablename__ = "workflow_step_execution"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Enum(ExecutionStatus), nullable=False, default=ExecutionStatus.PENDING)
    result = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    # Snapshot of step.name at execution time (revision c2e_step_exec_snapshot).
    # Historical run views must render from this when the step itself has been
    # deleted (step_id NULL) or renamed since — the run-detail view's
    # degraded-mode fallback (doc 18 automations-ux §2 Fix B).
    step_name = Column(String, nullable=True)

    # --- engine v2 (wf1) ---
    node_key = Column(String(32), nullable=True)
    action_key = Column(String(64), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    simulated = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    input = Column(JSON, nullable=True)  # the resolved config
    error_code = Column(String(48), nullable=True)
    resume_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_step_execution_node", "execution_id", "node_key", unique=True,
              postgresql_where=text("node_key IS NOT NULL"),
              sqlite_where=text("node_key IS NOT NULL")),
        # The C5 message-cap count.
        Index("ix_step_execution_messages", "action_key", "completed_at"),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_execution.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable + SET NULL (was NOT NULL/CASCADE): deleting a step must not
    # erase its run history. step_name above is what historical views render
    # once step_id is NULL.
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_step.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    execution = relationship("WorkflowExecution", back_populates="step_executions")
    step = relationship("WorkflowStep", back_populates="step_executions")


class WorkflowVersion(Base):
    """One version of a v2 workflow's definition. At most one DRAFT per
    workflow; publishing turns the draft PUBLISHED and the previous active one
    SUPERSEDED. Published versions are never edited (runs copy the definition
    anyway). status/trigger_type CHECKs live in the revision."""
    __tablename__ = "workflow_version"
    __table_args__ = (
        UniqueConstraint("workflow_id", "number", name="uq_workflow_version_number"),
        Index("uq_workflow_version_draft", "workflow_id", unique=True,
              postgresql_where=text("status = 'DRAFT'"), sqlite_where=text("status = 'DRAFT'")),
        Index("ix_workflow_version_trigger", "company_id", "trigger_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)
    published_at = Column(DateTime(timezone=True), nullable=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number = Column(Integer, nullable=False)
    status = Column(
        Enum(WorkflowVersionStatus, native_enum=False, length=16, create_constraint=False),
        nullable=False, default=WorkflowVersionStatus.DRAFT,
    )
    definition = Column(_JSONB, nullable=False)
    # Denormalized from definition.trigger for the consumer's match query.
    trigger_type = Column(String(16), nullable=False)  # WORKFLOW_TRIGGER_TYPES
    trigger_key = Column(String(64), nullable=False)
    # Set on a draft by template-update apply / "Editar parámetros"; copied to
    # workflow.source_template_version on publish.
    source_template_version = Column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    workflow = relationship("Workflow", foreign_keys=[workflow_id], back_populates="versions")


class AutomationTemplate(Base):
    """A global (no company_id) workflow template version, seeded by backend-erp
    from `workflows/templates/<key>.v<N>.json`. Immutable once seeded: a file
    whose checksum differs from the stored row is logged, never overwritten.
    Not named workflow_template: ld1_legacy_drop dropped that table."""
    __tablename__ = "automation_template"
    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_automation_template_key_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    key = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    name = Column(JSON, nullable=False)          # {es, en}
    description = Column(JSON, nullable=True)    # {es, en}
    category = Column(String(32), nullable=False)
    changelog = Column(JSON, nullable=False, default=list)  # [{version, es, en}]
    params = Column(JSON, nullable=False, default=list)     # param specs
    requires = Column(JSON, nullable=False, default=dict)   # {purposes?, integrations?}
    definition = Column(_JSONB, nullable=False)
    checksum = Column(String(64), nullable=False)  # sha256 of the canonical JSON


class WorkflowEvent(Base):
    """Business-event outbox for the v2 engine (a copy of provisioning_event).
    Core code inserts a row in the same transaction as the state change
    (`utils/workflow_events.emit_business_event`, never commits); the
    WorkflowWorker drains it (advisory lock, id order), matches workflows and
    stamps processed_at. `depth` / `source_run_id` carry the loop guard."""
    __tablename__ = "workflow_event"
    __table_args__ = (
        Index("ix_workflow_event_pending", "id",
              postgresql_where=text("processed_at IS NULL"),
              sqlite_where=text("processed_at IS NULL")),
    )

    # bigserial on Postgres; SQLite only autoincrements an INTEGER PK.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(64), nullable=False)
    payload = Column(_JSONB, nullable=False)
    depth = Column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    source_run_id = Column(Uuid, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt,
                        server_default=text("now()"))
    processed_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error = Column(Text, nullable=True)
