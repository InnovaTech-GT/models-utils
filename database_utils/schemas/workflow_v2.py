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

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from database_utils.models.workflow import ExecutionStatus, WorkflowVersionStatus

__all__ = [
    "NODE_KEY_PATTERN",
    "BotMessage",
    "ChatOption",
    "ConversationEntry",
    "DefinitionProblem",
    "DraftIn",
    "DraftRef",
    "EdgeDef",
    "HandOff",
    "InstallIn",
    "LastRunRef",
    "MessageCapIn",
    "NodeDef",
    "ParamsIn",
    "RunCounts",
    "RunReplyIn",
    "RunTurnOut",
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
    # wf2 (D21): a protected core step from a template; only the action's
    # editable keys may change. Omitted from dumps when false, so a phase 1
    # definition's canonical dump is unchanged.
    locked: bool = False

    @model_serializer(mode="wrap")
    def _omit_unlocked(self, handler):
        data = handler(self)
        if not self.locked:
            data.pop("locked", None)
        return data


class EdgeDef(_Strict):
    from_: str = Field(alias="from")
    to: str
    # completed (default): follow only when the step COMPLETED; always: also on SKIPPED.
    on: Literal["completed", "always"] = "completed"
    # The source step's branch: yes/no (condition), an ask option id,
    # answered/invalid/timeout, verified, ... Which names exist is the catalog's call.
    branch: str | None = Field(default=None, pattern=NODE_KEY_PATTERN)


class WorkflowDefinition(_Strict):
    """Dump with `model_dump(by_alias=True)` (keys `schema`, `from`)."""
    schema_version: Literal[1, 2] = Field(default=1, alias="schema")
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
    severity: Literal["error", "warning"] = "error"


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
    branch: str | None = None  # result.branch of a branching step (wf2)
    messages_sent: int = 0


# --- conversations (wf2, doc 64 / plan 64a §2.4) ---

MessageKind = Literal["text", "options", "buttons"]


class ChatOption(BaseModel):
    id: str
    title: str
    description: str | None = None


class ConversationEntry(BaseModel):
    """One line of `context.transcript`, as the run history shows it."""
    seq: int
    dir: Literal["out", "in"]
    node_key: str | None = None
    kind: MessageKind = "text"
    text: str
    options: list[ChatOption] | None = None
    at: datetime
    redacted: bool = False


class BotMessage(BaseModel):
    """One outbound chat message for the gateway to render."""
    type: MessageKind = "text"
    text: str
    button: str | None = None
    options: list[ChatOption] | None = None


class HandOff(BaseModel):
    receipt_link_id: str | None = None
    flow: Literal["ya_pague", "menu", "agente"] | None = None


class RunTurnOut(BaseModel):
    """A conversational turn: the bot endpoints and the test-reply endpoint.
    `handled=False` (nothing waits on the chat) carries no run."""
    handled: bool = True
    run_id: UUID | None = None
    status: ExecutionStatus | None = None
    finished: bool = False
    ok: bool = False
    awaiting: bool = False
    wait_seconds: int | None = None
    pending: bool = False
    seq: int = 0  # highest out-entry seq in `messages`; the gateway acks it next call
    messages: list[BotMessage] = Field(default_factory=list)
    hand_off: HandOff | None = None


class RunReplyIn(_Strict):
    """A client's answer: free text or an option id, exactly one."""
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    # 65 = rendered interactive id "{node_key}.{option_id}" (32 + 1 + 32)
    option_id: str | None = Field(default=None, min_length=1, max_length=65)

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.text is None) == (self.option_id is None):
            raise ValueError("send exactly one of text or option_id")
        return self


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
    conversation: list[ConversationEntry] = Field(default_factory=list)
    awaiting: bool = False  # WAITING on a client reply


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
    requires: dict[str, Any] = Field(default_factory=dict)  # e.g. {"types": ["CONVERSATION"]}


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
    channel: Literal["whatsapp", "telegram"] | None = None


class MessageCapIn(_Strict):
    daily_cap: int | None = Field(default=None, ge=0, le=100000)  # None = default (500)
