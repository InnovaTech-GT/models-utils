from sqlalchemy import (
    Column, String, Boolean, JSON, DateTime, ForeignKey, Enum, Uuid, Float
)
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


class WorkflowTemplate(Base):
    """Installable workflow blueprint (ADR-007). Global rows (seeded); a company
    'installs' one to materialize a Workflow + triggers + steps + edges with
    parameters (task column IDs, playbook IDs, ...) resolved at install time.

    definition JSON shape:
    {
      "parameters": [{"key": "install_state_id", "label": "...", "type": "task_state",
                      "required": true}],
      "triggers":   [ ...WorkflowTrigger fields with {{param}} placeholders... ],
      "steps":      [ {"ref": "s1", ...WorkflowStep fields...} ],
      "edges":      [ {"from": "s1", "to": "s2"} ]
    }
    """
    __tablename__ = "workflow_template"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    key = Column(String, nullable=False, unique=True)  # "new-installation", "onu-replacement"
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    category = Column(String, nullable=True)  # "installation", "billing", "network"
    definition = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)


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

    # Relationships
    company = relationship("Company", back_populates="workflows")
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
    # {"resource_type": "task", "data": {"name": "Follow up", "task_state_id": "uuid"}}

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

    # Relationships
    workflow = relationship("Workflow", back_populates="executions")
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
