from pydantic import BaseModel, ConfigDict
from typing import Literal, Optional, List
from uuid import UUID
from datetime import datetime
from database_utils.models.crm import TaskStateColor

# tk2_task_links: mirrors TASK_STATE_KINDS / ck_task_state_kind. A Literal
# (not an enum) because the column is a CHECK-constrained string — an
# unknown value must 422 at the API, never reach the CHECK.
TaskStateKind = Literal["ASSIGNED", "IN_PROGRESS", "DONE", "CANCELLED"]


class TaskStateBase(BaseModel):
    name: str
    color: TaskStateColor = TaskStateColor.GRAY
    position: int = 0
    kind: TaskStateKind = "IN_PROGRESS"


class TaskStateCreate(TaskStateBase):
    pass


class TaskStateUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[TaskStateColor] = None
    position: Optional[int] = None
    kind: Optional[TaskStateKind] = None


class TaskStateOut(TaskStateBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    updated_at: datetime
    task_count: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class TaskStateReorder(BaseModel):
    ordered_ids: List[UUID]
