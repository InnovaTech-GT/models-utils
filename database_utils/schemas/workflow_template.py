# schemas/workflow_template.py
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime


class WorkflowTemplateOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    definition: Dict[str, Any]
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowTemplateParameter(BaseModel):
    """Parameter descriptor surfaced to the install UI."""
    key: str
    label: str
    type: str  # "task_state" | "playbook" | "user" | "string" | "number"
    required: bool = True
    description: Optional[str] = None


class WorkflowTemplateInstallRequest(BaseModel):
    # Values for the template's parameters, keyed by parameter key.
    parameters: Dict[str, Any] = {}
    # Optional override for the created workflow's name.
    workflow_name: Optional[str] = None
    activate: bool = True


class WorkflowTemplateInstallResult(BaseModel):
    workflow_id: UUID
    workflow_name: str
    steps_created: int
    triggers_created: int
    edges_created: int
