# schemas/playbook.py
"""
Declarative playbook format (ADR-005/006).

A playbook definition is uploadable JSON (or YAML converted client-side):

{
  "variables": [
    {"key": "onu_serial", "label": "ONU Serial", "type": "TEXT", "required": true},
    {"key": "vlan", "label": "Service VLAN", "type": "NUMBER", "required": true}
  ],
  "steps": [
    {
      "name": "register-onu",
      "driver": "simulator",           // simulator | http | ssh | telnet | snmp | tr069 | ping
      "template": "interface gpon 0/1\\n ont add {{onu_serial}} vlan {{vlan}}",
      "request": null,                  // http driver: {"method","path","headers","body"}
      "validation": {"expect_contains": "success", "expect_status": 200},
      "timeout_seconds": 30
    }
  ],
  "rollback": [ ...same step shape... ]
}

Templates use {{variable}} substitution only — no expressions, no code execution.
"""
import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import (
    PLAYBOOK_PURPOSE_PATTERN,
    ProvisioningJobStatus,
    ProvisioningTrigger,
)


def normalize_purpose(v: str) -> str:
    """strip -> upper -> regex-validate.

    Deliberately module-level and importable: the provision endpoint body
    schema (ClientServiceProvisionIn), the binding endpoints, and the engine's
    ENQUEUE_PROVISIONING config path all share this exact normalization, so a
    tenant typing 'Activation' or 'activation ' always matches the seeded
    'ACTIVATION' binding (doc 20a verifier fix on purpose string matching).

    Moved here from the deleted schemas/topology.py in Cycle 10 — purposes now
    key playbook bindings, not topologies (doc 35 §2.4).
    """
    p = v.strip().upper().replace(' ', '_').replace('-', '_')
    if not re.match(PLAYBOOK_PURPOSE_PATTERN, p):
        raise ValueError(f"purpose must match {PLAYBOOK_PURPOSE_PATTERN} (got '{v}')")
    return p

# Cycle 7 (doc 25 §4.3): "ping" joins the set — backend-erp's connectivity
# probe driver (provisioning/drivers/ping.py), used by the per-company
# core_connectivity_check system playbooks (doc 25 §5.1).
PLAYBOOK_DRIVERS = {"simulator", "http", "ssh", "telnet", "snmp", "tr069", "ping"}
_VAR_TYPES = {"TEXT", "NUMBER", "BOOLEAN"}


class PlaybookVariable(BaseModel):
    key: str
    label: Optional[str] = None
    type: str = "TEXT"
    required: bool = False
    default: Optional[Any] = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError("variable key must be snake_case")
        return v

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in _VAR_TYPES:
            raise ValueError(f"variable type must be one of {sorted(_VAR_TYPES)}")
        return v


class PlaybookStepValidation(BaseModel):
    expect_contains: Optional[str] = None
    expect_not_contains: Optional[str] = None
    expect_status: Optional[int] = None  # http driver


class PlaybookPrecondition(BaseModel):
    """Check-before-create guard (canon C15, doc 21 §3.6). The executor runs
    the template/request as a read, evaluates `validation`, then:
    when_met='skip' -> idempotent no-op (step already satisfied);
    when_met='fail' -> abort the job (guard breached). The existing step-level
    `validation` block stays the POSTcondition — there is no new postcondition
    field (canon C15)."""
    template: Optional[str] = None
    request: Optional[Dict[str, Any]] = None
    validation: PlaybookStepValidation  # REQUIRED: what "already satisfied" looks like
    when_met: str = "skip"
    # Cycle 7 (doc 25 §4.1): same step-targeting surface as PlaybookStep —
    # a precondition may probe the concrete device the action targets.
    target_item_id: Optional[str] = None

    @field_validator("when_met")
    @classmethod
    def validate_when_met(cls, v: str) -> str:
        if v not in {"skip", "fail"}:
            raise ValueError("precondition when_met must be 'skip' or 'fail'")
        return v

    @model_validator(mode="after")
    def validate_payload(self) -> "PlaybookPrecondition":
        if not self.template and not self.request:
            raise ValueError("precondition requires 'template' or 'request'")
        return self


class PlaybookStep(BaseModel):
    name: str
    driver: str
    template: Optional[str] = None            # command-style drivers
    request: Optional[Dict[str, Any]] = None  # http / tr069 drivers
    validation: Optional[PlaybookStepValidation] = None  # postcondition (canon C15)
    timeout_seconds: int = 30
    # Cycle 7 (doc 25 §4.1): step targeting for the CLI/ping drivers — an
    # inventory_item id or a "{{variable}}" the executor renders with the job
    # variables. Declared here so it round-trips through model_dump()/
    # PlaybookOut instead of being silently dropped.
    #
    # Cycle 10 (doc 35 §4.4): target_position is GONE. A playbook now binds to
    # one device type and therefore runs on exactly one device, so there is no
    # chain slot left to address; the executor defaults the target to
    # {{device.item_id}} and target_item_id remains the power-user override.
    target_item_id: Optional[str] = None
    # --- Cycle 5 Phase 1 additive fields (canon C15) ---
    precondition: Optional[PlaybookPrecondition] = None
    # per-step compensation (saga-lite, doc 21 §3.7). Depth-1 only: an
    # on_failure step may not itself carry on_failure or a precondition.
    on_failure: List["PlaybookStep"] = []

    @field_validator("driver")
    @classmethod
    def validate_driver(cls, v: str) -> str:
        if v not in PLAYBOOK_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(PLAYBOOK_DRIVERS)}")
        return v

    @model_validator(mode="after")
    def validate_payload(self) -> "PlaybookStep":
        # canon C15: tr069 accepts a `request` dict like http. It ALSO still
        # accepts `template` so pre-Cycle-5 tr069 (stub) definitions stay valid
        # — the relaxation only adds the request path, never removes template.
        if self.driver == "http":
            if not self.request:
                raise ValueError(f"step '{self.name}': http driver requires 'request'")
        elif self.driver == "tr069":
            if not self.request and not self.template:
                raise ValueError(
                    f"step '{self.name}': tr069 driver requires 'request' or 'template'"
                )
        elif not self.template:
            raise ValueError(f"step '{self.name}': driver '{self.driver}' requires 'template'")
        if not (1 <= self.timeout_seconds <= 600):
            raise ValueError(f"step '{self.name}': timeout_seconds must be 1-600")
        # Compensation steps are depth-1: forbid nested on_failure/precondition.
        for comp in self.on_failure:
            if comp.on_failure:
                raise ValueError(
                    f"step '{self.name}': on_failure steps must not nest on_failure"
                )
            if comp.precondition is not None:
                raise ValueError(
                    f"step '{self.name}': on_failure steps must not carry a precondition"
                )
        return self


PlaybookStep.model_rebuild()  # resolve the self-referential on_failure forward ref


class PlaybookDefinition(BaseModel):
    variables: List[PlaybookVariable] = []
    steps: List[PlaybookStep]
    rollback: List[PlaybookStep] = []

    @model_validator(mode="after")
    def validate_definition(self) -> "PlaybookDefinition":
        if not self.steps:
            raise ValueError("playbook must have at least one step")
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique")
        return self


class PlaybookBase(BaseModel):
    name: str
    description: Optional[str] = None
    # Cycle 8 dropped target_vendor/target_category; Cycle 10 (doc 35 §2.4)
    # replaced topology ownership with device_type_playbook /
    # inventory_item_playbook bindings, so a playbook row is once again a plain
    # company-scoped library entry with no ownership column of its own.
    is_active: bool = True
    definition: PlaybookDefinition


class PlaybookCreate(PlaybookBase):
    pass


class PlaybookUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    definition: Optional[PlaybookDefinition] = None


class PlaybookOut(PlaybookBase):
    id: UUID
    company_id: UUID
    version: int
    created_at: datetime
    # Cycle 5 Phase 1 (canon C7): the playbook version whose dry-run last
    # SUCCEEDED. A live job is accepted iff this equals `version`.
    last_dry_run_version: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# --- ProvisioningJob ---

class ProvisioningJobCreate(BaseModel):
    playbook_id: UUID
    variables: Optional[Dict[str, Any]] = None
    client_service_id: Optional[UUID] = None
    inventory_item_id: Optional[UUID] = None
    integration_id: Optional[UUID] = None
    idempotency_key: Optional[str] = None
    max_attempts: int = 3
    scheduled_for: Optional[datetime] = None
    # Cycle 5 Phase 1 (canon C7): a dry-run job never touches a device; a
    # SUCCEEDED dry-run stamps playbook.last_dry_run_version.
    dry_run: bool = False


class ProvisioningJobOut(BaseModel):
    id: UUID
    company_id: UUID
    playbook_id: UUID
    status: ProvisioningJobStatus
    attempts: int
    max_attempts: int
    idempotency_key: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    variables: Optional[Dict[str, Any]] = None
    log: Optional[Any] = None
    error: Optional[str] = None
    triggered_by: ProvisioningTrigger
    triggered_by_user_id: Optional[UUID] = None
    client_service_id: Optional[UUID] = None
    inventory_item_id: Optional[UUID] = None
    integration_id: Optional[UUID] = None
    created_at: datetime
    playbook: Optional[PlaybookOut] = None
    # --- Cycle 5 Phase 1 (network config) ---
    dry_run: bool = False
    pending_step_index: Optional[int] = None    # canon C2: parked step (PENDING_INFORM)
    pending_task_ids: Optional[Any] = None      # GenieACS task ids polled on the 202 path
    heartbeat_at: Optional[datetime] = None     # canon C11: lease reaper
    device_lock_key: Optional[str] = None       # canon C11: per-device serialization key

    model_config = ConfigDict(from_attributes=True)
