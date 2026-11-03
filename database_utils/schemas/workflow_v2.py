"""Workflow engine v2 (doc 63-workflow-engine): the definition JSON and the
`/automations` API I/O. Shared by backend-erp, the bot endpoints and the MCP
docs.

The definition models check STRUCTURE only. Graph and catalog rules (unique
keys, <= 50 nodes, edges to known nodes, acyclic, branches, known
actions/variables) are backend-erp `workflows/definition.validate`, which
returns them as `problems` so a draft can be saved while incomplete.
"""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from database_utils.models.workflow import ExecutionStatus, WorkflowVersionStatus

__all__ = [
    "NODE_KEY_PATTERN",
    "DefinitionProblem",
    "DraftIn",
    "DraftRef",
    "EdgeDef",
    "InstallIn",
    "LastRunRef",
    "MessageCapIn",
    "NodeDef",
    "ParamsIn",
    "RunCounts",
    "SourceTemplateRef",
    "TemplateOut",
    "TemplatePreviewIn",
    "TestRunIn",
    "TriggerDef",
    "TriggerRef",
    "VersionRef",
    "WorkflowDefinition",
    "WorkflowRunDetail",
    "WorkflowRunOut",
    "WorkflowStepRunOut",
    "WorkflowV2CreateIn",
    "WorkflowV2Detail",
    "WorkflowV2Out",
    "WorkflowV2PatchIn",
    "WorkflowVersionDetail",
    "WorkflowVersionOut",
]

NODE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
TriggerType = Literal["event", "date", "bot"]
Locale = Literal["es", "en"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- definition (stored in workflow_version.definition, schema 1) ---

class TriggerDef(_Strict):
    type: TriggerType
    key: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class NodeDef(_Strict):
    key: str = Field(pattern=NODE_KEY_PATTERN)
    action: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)


class EdgeDef(_Strict):
    from_: str = Field(alias="from")
    to: str
    # completed (default): follow only when the step COMPLETED; always: also on SKIPPED.
    on: Literal["completed", "always"] = "completed"
    branch: Literal["yes", "no"] | None = None  # condition nodes only


class WorkflowDefinition(_Strict):
    """Dump with `model_dump(by_alias=True)` (keys `schema`, `from`)."""
    schema_version: Literal[1] = Field(default=1, alias="schema")
    trigger: TriggerDef
    start: str | None = None  # None only on a trigger-only draft
    nodes: list[NodeDef] = Field(default_factory=list)
    edges: list[EdgeDef] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    customer_reply: str | None = Field(default=None, max_length=2000)  # bot triggers only


class DefinitionProblem(BaseModel):
    path: str
    code: str
    message: str


# --- workflows ---

class TriggerRef(BaseModel):
    type: TriggerType
    key: str


class VersionRef(BaseModel):
    number: int
    published_at: datetime | None = None


class DraftRef(BaseModel):
    number: int
    updated_at: datetime


class SourceTemplateRef(BaseModel):
    key: str
    version: int | None = None
    latest_version: int | None = None
    update_available: bool = False


class LastRunRef(BaseModel):
    id: UUID
    status: ExecutionStatus
    started_at: datetime | None = None


class RunCounts(BaseModel):
    completed: int = 0
    failed: int = 0
    waiting: int = 0


class WorkflowV2Out(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    is_active: bool
    trigger: TriggerRef | None = None
    active_version: VersionRef | None = None
    draft_version: DraftRef | None = None
    source_template: SourceTemplateRef | None = None
    last_run: LastRunRef | None = None
    counts_7d: RunCounts = Field(default_factory=RunCounts)


class WorkflowV2Detail(WorkflowV2Out):
    active_definition: dict[str, Any] | None = None
    draft_definition: dict[str, Any] | None = None
    draft_problems: list[DefinitionProblem] = Field(default_factory=list)
    template_params: dict[str, Any] | None = None
    customer_reply: str | None = None


class WorkflowV2CreateIn(_Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    trigger: TriggerDef


class WorkflowV2PatchIn(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class WorkflowVersionOut(BaseModel):
    number: int
    status: WorkflowVersionStatus
    published_at: datetime | None = None
    published_by_name: str | None = None
    created_at: datetime


class WorkflowVersionDetail(BaseModel):
    number: int
    status: WorkflowVersionStatus
    definition: dict[str, Any]


# --- runs ---

class WorkflowStepRunOut(BaseModel):
    node_key: str | None = None
    name: str | None = None
    action_key: str | None = None
    status: ExecutionStatus
    simulated: bool = False
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    resume_at: datetime | None = None
    input: Any | None = None
    output: Any | None = None
    error_code: str | None = None
    error: str | None = None


class WorkflowRunOut(BaseModel):
    id: UUID
    version_number: int | None = None
    status: ExecutionStatus
    is_test: bool = False
    trigger_key: str | None = None
    trigger_summary: str | None = None
    client_id: UUID | None = None
    client_name: str | None = None
    client_service_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    resume_at: datetime | None = None
    error_code: str | None = None
    error: str | None = None


class WorkflowRunDetail(WorkflowRunOut):
    trigger: dict[str, Any] | None = None
    steps: list[WorkflowStepRunOut] = Field(default_factory=list)
    definition: dict[str, Any] | None = None  # the run's frozen snapshot


# --- templates ---

class TemplateOut(BaseModel):
    key: str
    version: int
    name: dict[str, str]
    description: dict[str, str] | None = None
    category: str
    params: list[dict[str, Any]] = Field(default_factory=list)
    changelog: list[dict[str, Any]] = Field(default_factory=list)
    installed_workflow_ids: list[UUID] = Field(default_factory=list)
    definition: dict[str, Any] | None = None  # GET /templates/{key} only


class TemplatePreviewIn(_Strict):
    params: dict[str, Any] = Field(default_factory=dict)
    locale: Locale = "es"


class InstallIn(TemplatePreviewIn):
    activate: bool = False


class DraftIn(_Strict):
    definition: WorkflowDefinition


class ParamsIn(_Strict):
    params: dict[str, Any]


class TestRunIn(_Strict):
    version: Literal["draft", "active"] = "draft"
    client_service_id: UUID | None = None
    client_id: UUID | None = None
    trigger_overrides: dict[str, Any] | None = None
    inputs: dict[str, Any] | None = None
    send_to_me: bool = False


class MessageCapIn(_Strict):
    daily_cap: int | None = Field(default=None, ge=0, le=100000)  # None = default (500)
