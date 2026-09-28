from pydantic import BaseModel, ConfigDict
from typing import Literal, Optional, List
from uuid import UUID
from datetime import datetime

# Single source of truth — the schema previously duplicated this enum and
# silently drifted when new members were added. NETWORK_NODE was removed from
# the Python enum in Cycle 2 (revision c2d_graph_removal); the PG enum VALUE
# stays forever (Postgres cannot DROP a label) but no schema/model code
# references it anymore.
from database_utils.models.crm import TaskLinkedObjectType

# ts1_task_status: mirrors TASK_STATUSES / ck_task_status. A Literal so an
# unknown value 422s at the API instead of reaching the CHECK.
TaskStatus = Literal["PENDING", "ASSIGNED", "IN_PROGRESS", "DONE"]


class TaskAssigneeSimple(BaseModel):
    id: UUID
    name: str
    email: str

    model_config = ConfigDict(from_attributes=True)


class TaskBase(BaseModel):
    name: str
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    linked_object_type: Optional[TaskLinkedObjectType] = None
    linked_object_id: Optional[UUID] = None


class TaskCreate(TaskBase):
    # Omitted or PENDING/ASSIGNED: derived from the technician assignment.
    status: Optional[TaskStatus] = None
    task_state_id: Optional[UUID] = None
    assignee_ids: Optional[List[UUID]] = []
    position: Optional[int] = None
    time_spent_minutes: Optional[int] = None


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[TaskStatus] = None
    task_state_id: Optional[UUID] = None
    assignee_ids: Optional[List[UUID]] = None
    position: Optional[int] = None
    linked_object_type: Optional[TaskLinkedObjectType] = None
    linked_object_id: Optional[UUID] = None
    time_spent_minutes: Optional[int] = None


class TaskOut(TaskBase):
    id: UUID
    company_id: UUID
    status: TaskStatus = "PENDING"
    task_state_id: Optional[UUID] = None
    position: int
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    assignees: List[TaskAssigneeSimple] = []
    creator: Optional[TaskAssigneeSimple] = None
    time_spent_minutes: Optional[int] = None
    # Written only by the dispatch ETL; read-only for every other client.
    route_sequence: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class TaskMove(BaseModel):
    status: TaskStatus
    position: Optional[int] = None


class TaskBulkReorder(BaseModel):
    status: TaskStatus
    ordered_task_ids: List[UUID]
