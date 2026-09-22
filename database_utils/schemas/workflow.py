from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from database_utils.models.workflow import TriggerEventType, StepActionType, ExecutionStatus


# --- Workflow ---

class WorkflowBase(BaseModel):
    name: str
    description: Optional[str] = None


class WorkflowCreate(WorkflowBase):
    pass


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class WorkflowOut(WorkflowBase):
    id: UUID
    is_active: bool
    company_id: UUID
    created_at: datetime
    updated_at: datetime
    trigger_count: Optional[int] = 0
    step_count: Optional[int] = 0

    model_config = ConfigDict(from_attributes=True)


class WorkflowDetailOut(WorkflowOut):
    triggers: List["WorkflowTriggerOut"] = []
    steps: List["WorkflowStepOut"] = []
    edges: List["WorkflowStepEdgeOut"] = []


# --- Trigger ---

class WorkflowTriggerBase(BaseModel):
    resource_type: str
    event_type: TriggerEventType
    field_conditions: Optional[dict] = None


class WorkflowTriggerCreate(WorkflowTriggerBase):
    pass


class WorkflowTriggerUpdate(BaseModel):
    resource_type: Optional[str] = None
    event_type: Optional[TriggerEventType] = None
    field_conditions: Optional[dict] = None


class WorkflowTriggerOut(WorkflowTriggerBase):
    id: UUID
    workflow_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Step ---

class WorkflowStepBase(BaseModel):
    name: str
    description: Optional[str] = None
    action_type: StepActionType
    action_config: dict
    position_x: Optional[float] = 0
    position_y: Optional[float] = 0


class WorkflowStepCreate(WorkflowStepBase):
    pass


class WorkflowStepUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    action_type: Optional[StepActionType] = None
    action_config: Optional[dict] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None


class WorkflowStepOut(WorkflowStepBase):
    id: UUID
    workflow_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Edge ---

class WorkflowStepEdgeCreate(BaseModel):
    from_step_id: UUID
    to_step_id: UUID


class WorkflowStepEdgeOut(BaseModel):
    id: UUID
    from_step_id: UUID
    to_step_id: UUID
    workflow_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Graph (bulk update for visual editor) ---

class WorkflowStepInGraph(BaseModel):
    id: Optional[UUID] = None  # None = new step
    name: str
    description: Optional[str] = None
    action_type: StepActionType
    action_config: dict
    position_x: Optional[float] = 0
    position_y: Optional[float] = 0


class WorkflowEdgeInGraph(BaseModel):
    from_step_index: int  # Index into the steps array
    to_step_index: int


class WorkflowGraphUpdate(BaseModel):
    steps: List[WorkflowStepInGraph]
    edges: List[WorkflowEdgeInGraph]


# --- Execution ---

class WorkflowExecutionOut(BaseModel):
    id: UUID
    workflow_id: UUID
    status: ExecutionStatus
    trigger_event: dict
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowStepExecutionOut(BaseModel):
    id: UUID
    # Nullable since revision c2e_step_exec_snapshot: step_id is now
    # ON DELETE SET NULL (a deleted step must not erase its run history).
    # Historical run views render step_name below when this is None.
    step_id: Optional[UUID] = None
    step_name: Optional[str] = None
    status: ExecutionStatus
    result: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowExecutionDetailOut(WorkflowExecutionOut):
    step_executions: List[WorkflowStepExecutionOut] = []
