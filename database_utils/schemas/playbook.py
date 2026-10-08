# schemas/playbook.py
"""
Declarative playbook format (ADR-005/006), engine v2 (doc 42 §4).

One playbook = one purpose on one device type (or item). A definition holds
the phases directly, in the founder's layout:

{
  "variables": [...],            // input.* (unchanged)
  "computed":  [...],            // computed.*, declared integer arithmetic (doc 40)
  "session":   {"enable": {...}, "config_command": null, "exit_command": "exit",
                "error_patterns": null, "busy_patterns": []},   // ssh/telnet
  "secrets":   [{"key": "wifi_key", "length": 12}],              // secret.*, per RUN
  "preconditions": [Step],       // read-only checks; a failure ends the run, no rollback
  "configuration": [Step],       // was `steps`; >= 1 step
  "verification":  [Step],       // read-only post-checks with captures/thresholds
  "rollback":      [Step],       // this device's undo, gated per step by `undoes`
  "outputs": [{"key", "label", "value", "unit", "audience", "shareable", "sensitive"}]
}

A Step is {name, driver, template | request, validation, timeout_seconds,
target_item_id, precondition (configuration guard), label, hint, idempotent,
config_mode, capture, wait_until, undoes}; PlaybookDefinition checks which
phase accepts which field (PHASE_FIELD_NOT_ALLOWED).

The legacy `{steps, rollback}` shape (with per-step `on_failure`) is still
accepted: normalize_definition converts it on read and on save, so every save
stores v2 and stored legacy rows run unchanged (doc 42 §14.1).

Templates use {{variable}} substitution only — no expressions, no code execution.
Integer arithmetic is declared, never inline: an optional `computed` block
(doc 40 §3.3.3, utils/playbook_expr.py) whose results templates read by plain
lookup as {{computed.<key>}}. capture.* (filled at run time, same device) and
secret.* (generated per run) are never accepted from a caller.

Save-time error codes are the prefix of the ValueError message ("CODE: ..."):
CAPTURE_UNDECLARED, CAPTURE_SECRET_NAME, REGEX_UNSUPPORTED, UNDOES_UNKNOWN_STEP,
SECRET_UNDECLARED, OUTPUT_SECRET_MIXED, PHASE_FIELD_NOT_ALLOWED,
CONFIG_COMMAND_REQUIRED, OUTPUT_SHARE_AUDIENCE, LEGACY_STEPS_CONFLICT, COMPUTE_*.
WAIT_TOO_LONG_FOR_SHARED_DEVICE needs the binding: shared_device_wait_errors().
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from database_utils.models.isp import (
    PHASE_CONFIGURATION,
    PHASE_PRECONDITIONS,
    PHASE_ROLLBACK,
    PHASE_VERIFICATION,
    PLAYBOOK_PURPOSE_PATTERN,
    TEARDOWN_PURPOSES,
    ProvisioningJobStatus,
    ProvisioningTrigger,
)
from database_utils.utils import playbook_expr
from database_utils.utils.playbook_expr import is_secret_name


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


# --------------------------------------------------------------------------
# Engine v2 (doc 42 §4): phases, session, captures, secrets, outputs.
# --------------------------------------------------------------------------

# The four phase keys of a definition, in execution order, keyed by the
# PROVISIONING_PHASES value a run entry / child job carries.
PHASE_KEYS = {
    PHASE_PRECONDITIONS: "preconditions",
    PHASE_CONFIGURATION: "configuration",
    PHASE_VERIFICATION: "verification",
    PHASE_ROLLBACK: "rollback",
}
FORWARD_PHASE_KEYS = ("preconditions", "configuration", "verification")
# The synthetic preflight step (doc 42 §6.4): connect, log in, apply
# session.enable, disconnect. Reserved: no authored step may use the name.
SESSION_PROBE_STEP = "__session__"
SESSION_PROBE_LABEL = "Conexión con el equipo"
CLI_DRIVERS = frozenset({"ssh", "telnet"})
# Drivers whose step reaches a real device over the network (doc 42 §4.5).
NETWORK_DRIVERS = frozenset({"ssh", "telnet", "http", "tr069"})
# Drivers that change nothing that would need undoing (doc 42 §7.1).
NO_UNDO_DRIVERS = frozenset({"simulator", "ping"})

MAX_REGEX_LENGTH = 256
MAX_CAPTURES_PER_STEP = 8
MAX_CAPTURES_PER_PLAYBOOK = 32
MAX_OUTPUTS = 16
MAX_LABEL = 80
MAX_HINT = 200
WAIT_MAX_SECONDS = 600
# A wait_until poll holds its child's device lock; on a shared (non-CPE)
# device it is capped lower (doc 42 §8.1). Checked by the playbook router,
# which knows the bound device type's category: shared_device_wait_errors().
SHARED_DEVICE_WAIT_MAX_SECONDS = 120
OUTPUT_AUDIENCES = frozenset({"technician", "office"})
# Generated secrets: letters and digits without 0 O 1 l I (doc 42 §10.2).
SECRET_ALPHABET = "".join(
    c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    if c not in "0O1lI"
)

# Constructs RE2 (the execution engine, backend-erp) does not support, plus
# named groups (the capture value is "the one group"). Scanned with every
# escape except a backreference neutralised, so escaped text (`\(?=`, `\\1`)
# is not mistaken for a construct.
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_REGEX_REFUSED = (
    (re.compile(r"\(\?<?[=!]"), "lookaround"),
    (re.compile(r"\\[1-9]|\\g<|\(\?P="), "backreference"),
    (re.compile(r"\(\?P?<[A-Za-z_]"), "named group"),
)
_CAPTURE_REF = re.compile(r"[ \t]*capture\.([a-z][a-z0-9_]*)")
_SECRET_REF = re.compile(r"[ \t]*secret\.([a-z][a-z0-9_]*)")
_SECRET_ONLY = re.compile(r"\A\{\{[ \t]*secret\.([a-z][a-z0-9_]*)[ \t]*\}\}\Z")
_ONE_TOKEN = re.compile(r"\A\{\{[^{}\n]{1,512}\}\}\Z")
_DECIMAL = re.compile(r"\A-?\d+(?:\.\d+)?\Z")


def check_regex(pattern: str, where: str) -> "re.Pattern":
    """Save-time regex check (doc 42 §4.3). Tokens are compiled as a literal
    placeholder (they render escaped). backend-erp's router also runs
    re2.compile: the server is the authority."""
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ValueError(f"REGEX_UNSUPPORTED: {where}: longer than {MAX_REGEX_LENGTH} characters")
    body = playbook_expr.TOKEN_SHAPE.sub("x", pattern)
    scan = _ESCAPE.sub(lambda m: m.group(0) if m.group(1) in "123456789g" else "x", body)
    for rx, what in _REGEX_REFUSED:
        if rx.search(scan):
            raise ValueError(f"REGEX_UNSUPPORTED: {where}: {what} is not supported")
    try:
        return re.compile(body)
    except re.error as exc:
        raise ValueError(f"REGEX_UNSUPPORTED: {where}: {exc}") from None


def _static_text(value: Optional[str], limit: int, what: str) -> Optional[str]:
    if value is None:
        return value
    if len(value) > limit:
        raise ValueError(f"{what} must be at most {limit} characters")
    if "{{" in value or "}}" in value:
        raise ValueError(f"{what} must be static text (no token)")
    return value


class PlaybookStepValidation(BaseModel):
    """Every string is RENDERED with the job variables before comparing
    (doc 42 §4.3); tokens inside the regexes render escaped."""
    expect_contains: Optional[str] = None
    expect_not_contains: Optional[str] = None
    expect_regex: Optional[str] = None       # searched in the output
    expect_not_regex: Optional[str] = None
    expect_status: Optional[int] = None  # http driver

    @model_validator(mode="after")
    def validate_regexes(self) -> "PlaybookStepValidation":
        for field in ("expect_regex", "expect_not_regex"):
            value = getattr(self, field)
            if value is not None:
                check_regex(value, field)
        return self


Threshold = Optional[Union[StrictInt, float, str]]


def _check_threshold(value: Any, where: str, number: bool) -> None:
    if value is None or not isinstance(value, str):
        return
    if "{{" in value:
        if not _ONE_TOKEN.match(value):
            raise ValueError(f"{where}: a threshold is a literal or exactly ONE token")
    elif number and not _DECIMAL.match(value.strip()):
        raise ValueError(f"{where}: '{value}' is not a number")


class PlaybookCapture(BaseModel):
    """Value extraction with a typed threshold (doc 42 §4.3). No match fails
    the step CAPTURE_NOT_FOUND; a broken threshold THRESHOLD_VIOLATED."""
    key: str
    regex: str           # exactly one capture group: that group is the value
    type: str = "text"   # number (Decimal) | text
    label: Optional[str] = None
    unit: Optional[str] = None
    min: Threshold = None     # number: inclusive; a literal or ONE token
    max: Threshold = None
    equals: Optional[str] = None   # text: exact (literal or one token)

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not playbook_expr.KEY_PATTERN.fullmatch(v):
            raise ValueError("capture key must match ^[a-z][a-z0-9_]{0,31}$")
        if is_secret_name(v):
            raise ValueError(f"CAPTURE_SECRET_NAME: capture key '{v}' is secret-named")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v):
        return _static_text(v, MAX_LABEL, "capture label")

    @model_validator(mode="after")
    def validate_capture(self) -> "PlaybookCapture":
        where = f"capture '{self.key}'"
        if check_regex(self.regex, where).groups != 1:
            raise ValueError(f"{where}: the regex must have exactly one capture group")
        if self.type not in ("number", "text"):
            raise ValueError(f"{where}: type must be 'number' or 'text'")
        number = self.type == "number"
        if number and self.equals is not None:
            raise ValueError(f"{where}: 'equals' is for text captures")
        if not number and (self.min is not None or self.max is not None):
            raise ValueError(f"{where}: 'min'/'max' are for number captures")
        for name in ("min", "max", "equals"):
            _check_threshold(getattr(self, name), f"{where} {name}", number)
        lo, hi = self.min, self.max
        if (lo is not None and hi is not None
                and not isinstance(lo, str) and not isinstance(hi, str) and lo > hi):
            raise ValueError(f"{where}: min must not exceed max")
        return self


class PlaybookWaitUntil(BaseModel):
    """Condition polling, not error retry (doc 42 §4.2): a try is repeated
    only on VALIDATION_FAILED / CAPTURE_NOT_FOUND / THRESHOLD_VIOLATED (and,
    on a tr069 step, DEVICE_NOT_FOUND / CPE_NOT_CONNECTED)."""
    tries: int
    interval_seconds: int

    @model_validator(mode="after")
    def validate_bounds(self) -> "PlaybookWaitUntil":
        if not (2 <= self.tries <= 30):
            raise ValueError("wait_until.tries must be 2-30")
        if not (1 <= self.interval_seconds <= 60):
            raise ValueError("wait_until.interval_seconds must be 1-60")
        if self.tries * self.interval_seconds > WAIT_MAX_SECONDS:
            raise ValueError(f"wait_until: tries x interval_seconds must be <= {WAIT_MAX_SECONDS}")
        return self


class PlaybookPrecondition(BaseModel):
    """Check-before-create guard (canon C15, doc 21 §3.6). The executor runs
    the template/request as a read, evaluates `validation`, then:
    when_met='skip' -> idempotent no-op (step already satisfied);
    when_met='fail' -> abort the job (guard breached). The existing step-level
    `validation` block stays the POSTcondition — there is no new postcondition
    field (canon C15). Engine v2 keeps it as the configuration step guard
    ("Omitir si ya está aplicado"); it is not the preconditions PHASE."""
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
    # variables. Cycle 10 (doc 35 §4.4): target_position is GONE; the executor
    # defaults the target to {{device.item_id}}.
    target_item_id: Optional[str] = None
    # The configuration step guard (canon C15). on_failure (saga-lite) is
    # RETIRED in v2: normalize_definition turns it into rollback steps with
    # `undoes` (doc 42 §14.1).
    precondition: Optional[PlaybookPrecondition] = None
    # --- engine v2 (doc 42 §4.2); which phases accept which field is checked
    # by PlaybookDefinition (PHASE_FIELD_NOT_ALLOWED) ---
    label: Optional[str] = None       # technician-facing, static, <= 80; default = name
    hint: Optional[str] = None        # technician-safe remedy on failure, static, <= 200
    idempotent: bool = False          # resend-safe (doc 42 §8.2.4); was silently dropped
    config_mode: bool = False         # ssh/telnet: wrap in session.config_command/exit_command
    capture: List[PlaybookCapture] = []
    wait_until: Optional[PlaybookWaitUntil] = None
    undoes: Optional[str] = None      # rollback: runs only if that configuration step ran

    @field_validator("driver")
    @classmethod
    def validate_driver(cls, v: str) -> str:
        if v not in PLAYBOOK_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(PLAYBOOK_DRIVERS)}")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v):
        return _static_text(v, MAX_LABEL, "step label")

    @field_validator("hint")
    @classmethod
    def validate_hint(cls, v):
        return _static_text(v, MAX_HINT, "step hint")

    @model_validator(mode="after")
    def validate_payload(self) -> "PlaybookStep":
        # canon C15: tr069 accepts a `request` dict like http. It ALSO still
        # accepts `template` so pre-Cycle-5 tr069 (stub) definitions stay valid.
        # An empty template is legal (the session probe sends no line).
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
        if len(self.capture) > MAX_CAPTURES_PER_STEP:
            raise ValueError(
                f"step '{self.name}': at most {MAX_CAPTURES_PER_STEP} captures per step")
        return self


class PlaybookSessionEnable(BaseModel):
    """Privileged mode on every connect (doc 42 §9.3). The password is a
    CLI_ENABLE DeviceCredential, never part of the definition."""
    command: str = "enable"
    password_prompt: str = "ssword"   # regex
    enabled_prompt: str = "#"         # regex the prompt must end with

    @model_validator(mode="after")
    def validate_prompts(self) -> "PlaybookSessionEnable":
        check_regex(self.password_prompt, "session.enable.password_prompt")
        check_regex(self.enabled_prompt, "session.enable.enabled_prompt")
        return self


class PlaybookSession(BaseModel):
    """ssh/telnet session settings for every phase of this playbook."""
    enable: Optional[PlaybookSessionEnable] = None
    config_command: Optional[str] = None   # REQUIRED if any step sets config_mode
    exit_command: str = "exit"
    # re2 list; a match in a line's output = COMMAND_REJECTED. None = the
    # platform default (backend-erp); an explicit [] disables detection.
    error_patterns: Optional[List[str]] = None
    busy_patterns: List[str] = []          # a match = DEVICE_BUSY (transient)

    @model_validator(mode="after")
    def validate_patterns(self) -> "PlaybookSession":
        for field in ("error_patterns", "busy_patterns"):
            for i, pattern in enumerate(getattr(self, field) or []):
                check_regex(pattern, f"session.{field}[{i}]")
        return self


class PlaybookSecret(BaseModel):
    """A value generated once per RUN (doc 42 §10.2), encrypted on the run,
    read as {{secret.<key>}}. The alphabet is fixed (SECRET_ALPHABET)."""
    key: str
    length: int = 12

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not playbook_expr.KEY_PATTERN.fullmatch(v):
            raise ValueError("secret key must match ^[a-z][a-z0-9_]{0,31}$")
        return v

    @field_validator("length")
    @classmethod
    def validate_length(cls, v: int) -> int:
        if not (8 <= v <= 63):
            raise ValueError("secret length must be 8-63")
        return v


class PlaybookOutput(BaseModel):
    """A value published on the run (doc 42 §10.1)."""
    key: str
    label: str
    value: str          # template over resolver tokens, computed.*, capture.*, or exactly {{secret.<key>}}
    unit: Optional[str] = None
    audience: List[str]
    # The app may offer "Compartir por WhatsApp"; requires 'technician'.
    shareable: bool = False
    # Never returned by GET /runs/{id} nor put in a notification. Always true
    # (and refused as false) for a {{secret.*}} value.
    sensitive: bool = False

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not playbook_expr.KEY_PATTERN.fullmatch(v):
            raise ValueError("output key must match ^[a-z][a-z0-9_]{0,31}$")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v):
        return _static_text(v, MAX_LABEL, "output label")

    @model_validator(mode="after")
    def validate_output(self) -> "PlaybookOutput":
        where = f"output '{self.key}'"
        if not self.audience or not set(self.audience) <= OUTPUT_AUDIENCES:
            raise ValueError(f"{where}: audience must be a non-empty subset of "
                             f"{sorted(OUTPUT_AUDIENCES)}")
        if self.shareable and "technician" not in self.audience:
            raise ValueError(
                f"OUTPUT_SHARE_AUDIENCE: {where}: shareable needs 'technician' in audience")
        bodies = [m.group("body") for m in playbook_expr.TOKEN_SHAPE.finditer(self.value)]
        if any(_SECRET_REF.match(b) for b in bodies):
            if not _SECRET_ONLY.match(self.value):
                raise ValueError(
                    f"OUTPUT_SECRET_MIXED: {where}: a secret output is exactly "
                    "{{secret.<key>}}, with no other text or filter")
            if "sensitive" in self.model_fields_set and not self.sensitive:
                raise ValueError(f"{where}: a secret output is always sensitive")
            self.sensitive = True
        return self


def output_secret_ref(value: str) -> Optional[str]:
    """'secret.<key>' when an output value is a secret reference, else None."""
    m = _SECRET_ONLY.match(value or "")
    return f"secret.{m.group(1)}" if m else None


def mask_sensitive_outputs(outputs: Any) -> Any:
    """`value: null` on every `sensitive` / `secret` output entry (doc 42
    §10.1): such a value is never returned by the API, wherever the entry
    sits (`run.outputs` or a child's `log.outputs`). The flags are kept so the
    UI can show "••••"; the technician reads it through doc 43's audited path."""
    if not isinstance(outputs, list):
        return outputs
    return [dict(o, value=None) if isinstance(o, dict) and (o.get("sensitive") or o.get("secret"))
            else o for o in outputs]


def mask_log_outputs(log: Any) -> Any:
    """A job log with its `outputs` masked (mask_sensitive_outputs)."""
    if isinstance(log, dict) and log.get("outputs"):
        return dict(log, outputs=mask_sensitive_outputs(log["outputs"]))
    return log


class ComputedVar(BaseModel):
    """One declared integer value (doc 40 §3.3.3). `expr` is parsed here at
    save time and evaluated by the resolver and the renderer; the result is
    templated as {{computed.<key>}}."""
    key: str
    expr: str
    min: Optional[StrictInt] = None
    max: Optional[StrictInt] = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not playbook_expr.KEY_PATTERN.fullmatch(v):
            raise ValueError("computed key must match ^[a-z][a-z0-9_]{0,31}$")
        if is_secret_name(v):
            raise ValueError(f"COMPUTE_SECRET: computed key '{v}' is secret-named")
        return v

    @field_validator("expr")
    @classmethod
    def validate_expr(cls, v: str) -> str:
        playbook_expr.parse(v)  # ExprError is a ValueError -> 422
        return v

    @model_validator(mode="after")
    def validate_bounds(self) -> "ComputedVar":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"computed '{self.key}': min must not exceed max")
        return self


# The head of a {{computed.<key>}} token body, with the renderer's own
# leading-blank rule ([ \t]*). Matched against each raw step string's
# TOKEN_SHAPE bodies, never a JSON dump: JSON escapes a tab to `\t`.
_COMPUTED_HEAD = re.compile(r"[ \t]*computed\b")
# A well-formed computed token: exactly one segment, then optional blanks and
# an optional filter chain. `computed.onu.y` / `computed[0].x` are refused —
# they would never be produced by evaluate_all, so only a caller-supplied
# value could fill them (security review F1).
_COMPUTED_TOKEN = re.compile(r"[ \t]*computed\.([a-z][a-z0-9_]*)[ \t]*(?:\|.*)?\Z", re.S)


# --------------------------------------------------------------------------
# Legacy normalizer (doc 42 §14.1)
# --------------------------------------------------------------------------

def _canonical_steps(steps: Any) -> Any:
    """Steps as the API would dump them, so a mirror that went through the
    old editor compares equal even if it gained default keys."""
    try:
        return [PlaybookStep.model_validate(s).model_dump() for s in steps]
    except Exception:  # noqa: BLE001 — fall back to the raw comparison
        return steps


def normalize_definition(d: Any) -> Any:
    """Legacy `{steps, rollback}` -> v2 phases. Idempotent; v2 input is
    returned unchanged (minus an empty or mirrored `steps`).

    Precedence: `configuration` present and `steps` absent/empty -> v2;
    `configuration` absent -> legacy, converted; both non-empty -> `steps` is
    dropped when it equals `configuration` (the read-only mirror round-tripping),
    otherwise LEGACY_STEPS_CONFLICT.

    Runs in PlaybookDefinition (so every save stores v2) and in every reader of
    a stored definition (executor, worker, resolver, create_run's snapshot), so
    rows still stored legacy run correctly with no data rewrite.
    """
    if not isinstance(d, dict):
        return d
    steps = d.get("steps")
    if d.get("configuration") is not None:
        if steps and _canonical_steps(steps) != _canonical_steps(d["configuration"]):
            raise ValueError(
                "LEGACY_STEPS_CONFLICT: este playbook ya usa fases; edítelo en el nuevo editor")
        return {k: v for k, v in d.items() if k != "steps"}
    if steps is None:
        return d

    out = {k: v for k, v in d.items() if k not in ("steps", "rollback")}
    steps = [s for s in steps if isinstance(s, dict)]
    out["configuration"] = [{k: v for k, v in s.items() if k != "on_failure"} for s in steps]
    used = {s.get("name") for s in out["configuration"]}

    def unique(name):
        candidate, n = name, 1
        while candidate in used:
            n += 1
            candidate = f"{name} (rollback)" if n == 2 else f"{name} (rollback {n - 1})"
        used.add(candidate)
        return candidate

    if any(s.get("on_failure") for s in steps):
        # The old executor ran the failed step's on_failure, then the completed
        # steps' in reverse; with `undoes` the reverse walk covers the same
        # steps in the same order (the failed step is the last that "ran").
        out["rollback"] = [
            {k: v for k, v in comp.items() if k != "on_failure"}
            | {"undoes": s.get("name"), "name": unique(comp.get("name"))}
            for s in reversed(steps)
            for comp in (s.get("on_failure") or [])
            if isinstance(comp, dict)
        ]
    else:
        out["rollback"] = [
            dict(r) | {"name": unique(r.get("name"))}
            for r in (d.get("rollback") or []) if isinstance(r, dict)
        ]
    for key in ("preconditions", "verification", "outputs", "secrets"):
        out.setdefault(key, [])
    return out


def job_steps(definition: Any, phase: Optional[str], probe: bool = False) -> List[Dict[str, Any]]:
    """The step list a job executes (shared by the executor and the worker's
    recovery point, doc 42 §8.3). `phase` None = a standalone job:
    preconditions + configuration + verification flattened. `probe` prepends
    the synthetic session step to a PRECONDITIONS child (doc 42 §6.4)."""
    d = normalize_definition(definition or {}) or {}
    if phase is None:
        return [s for key in FORWARD_PHASE_KEYS for s in (d.get(key) or [])]
    steps = list(d.get(PHASE_KEYS[phase]) or [])
    if probe and phase == PHASE_PRECONDITIONS:
        driver = next((s.get("driver") for key in ("configuration", "verification")
                       for s in (d.get(key) or []) if s.get("driver") in CLI_DRIVERS),
                      "ssh")
        steps.insert(0, {"name": SESSION_PROBE_STEP, "label": SESSION_PROBE_LABEL,
                         "driver": driver, "template": "", "timeout_seconds": 30})
    return steps


def _wait_seconds(step: Dict[str, Any]) -> int:
    w = step.get("wait_until") or {}
    return int(w.get("tries") or 0) * int(w.get("interval_seconds") or 0)


def shared_device_wait_errors(definition: Any) -> List[Dict[str, Any]]:
    """WAIT_TOO_LONG_FOR_SHARED_DEVICE for every step whose poll exceeds the
    shared-device ceiling (doc 42 §8.1). The router calls it when the playbook
    is bound to a non-CPE device type."""
    d = normalize_definition(definition or {}) or {}
    return [
        {"code": "WAIT_TOO_LONG_FOR_SHARED_DEVICE", "phase": key, "step": s.get("name"),
         "detail": (f"step '{s.get('name')}': wait_until on a shared device may poll at most "
                    f"{SHARED_DEVICE_WAIT_MAX_SECONDS} s (tries x interval_seconds)")}
        for key in PHASE_KEYS.values()
        for s in (d.get(key) or [])
        if _wait_seconds(s) > SHARED_DEVICE_WAIT_MAX_SECONDS
    ]


def is_resend_safe(step: Dict[str, Any]) -> bool:
    """doc 42 §8.2.4: idempotent, guarded (when_met skip), or ping/simulator."""
    guard = step.get("precondition") or {}
    return bool(step.get("idempotent")
                or (guard and (guard.get("when_met") or "skip") == "skip")
                or step.get("driver") in NO_UNDO_DRIVERS)


def playbook_warnings(definition: Any, *, category_tier: Optional[str] = None,
                      purpose: Optional[str] = None) -> List[Dict[str, Any]]:
    """Save-time warnings, never errors (doc 42 §4.5): shown by the editor and
    the dry-run report. `category_tier` / `purpose` come from the binding."""
    d = normalize_definition(definition or {}) or {}
    cfg = [s for s in d.get("configuration") or [] if isinstance(s, dict)]
    rollback = [s for s in d.get("rollback") or [] if isinstance(s, dict)]
    warnings: List[Dict[str, Any]] = []
    if not rollback and any(s.get("driver") not in NO_UNDO_DRIVERS for s in cfg):
        warnings.append({"code": "ROLLBACK_EMPTY",
                         "detail": "Esta configuración no se puede revertir"})
    for s in rollback:
        if not s.get("undoes"):
            warnings.append({"code": "ROLLBACK_WITHOUT_UNDOES", "step": s.get("name"),
                             "detail": "Se ejecuta siempre que este equipo haya aplicado algún paso"})
    if not (d.get("session") or {}).get("enable") and any(
            s.get("driver") in CLI_DRIVERS
            and any(line.strip() == "enable" for line in (s.get("template") or "").splitlines())
            for s in cfg):
        warnings.append({"code": "ENABLE_WITHOUT_SESSION",
                         "detail": "Use session.enable (y una credencial CLI_ENABLE) en vez de un "
                                   "paso 'enable': cada paso abre su propia conexión"})
    for s in cfg:
        if not is_resend_safe(s):
            warnings.append({"code": "NOT_RESEND_SAFE", "step": s.get("name"),
                             "detail": "Este paso no se reintenta si se corta la conexión"})
    if (category_tier == "EDGE" and purpose is not None
            and purpose not in TEARDOWN_PURPOSES
            and any(s.get("driver") in NETWORK_DRIVERS for s in d.get("preconditions") or [])):
        warnings.append({"code": "CPE_NETWORK_PRECONDITION",
                         "detail": "Las precondiciones del CPE corren antes de que la OLT lo "
                                   "autorice; ponga las comprobaciones del CPE en verificación"})
    return warnings


def _refs(texts, pattern) -> List[str]:
    found = []
    for text in texts:
        for match in playbook_expr.TOKEN_SHAPE.finditer(text):
            ref = pattern.match(match.group("body"))
            if ref:
                found.append(ref.group(1))
    return found


class PlaybookDefinition(BaseModel):
    variables: List[PlaybookVariable] = []
    # Declared, not free-form: the library routes store model_dump(), which
    # would silently drop an undeclared key (doc 40 §3.3.3).
    computed: List[ComputedVar] = []
    session: Optional[PlaybookSession] = None
    secrets: List[PlaybookSecret] = []
    preconditions: List[PlaybookStep] = []
    configuration: List[PlaybookStep] = []
    verification: List[PlaybookStep] = []
    rollback: List[PlaybookStep] = []
    outputs: List[PlaybookOutput] = []
    # Read-only legacy mirror (= configuration), filled only on the way OUT
    # (PlaybookDefinitionOut) for the pre-doc-48 editor. Never stored: input
    # `steps` is consumed by normalize_definition.
    steps: Optional[List[PlaybookStep]] = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        return normalize_definition(data)

    @model_validator(mode="after")
    def validate_definition(self) -> "PlaybookDefinition":
        if not self.configuration:
            raise ValueError("playbook must have at least one configuration step")
        phases = [(key, getattr(self, key)) for key in PHASE_KEYS.values()]
        names = [s.name for _, steps in phases for s in steps]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique across all phases")
        if SESSION_PROBE_STEP in names:
            raise ValueError(f"step name '{SESSION_PROBE_STEP}' is reserved")
        self._validate_placement(phases)
        self._validate_captures_and_secrets(phases)
        self._validate_computed()
        return self

    def _validate_placement(self, phases) -> None:
        config_names = {s.name for s in self.configuration}
        session = self.session or PlaybookSession()
        for key, steps in phases:
            for s in steps:
                where = f"PHASE_FIELD_NOT_ALLOWED: {key} step '{s.name}'"
                if s.undoes is not None and key != "rollback":
                    raise ValueError(f"{where}: 'undoes' is rollback-only")
                if s.precondition is not None and key != "configuration":
                    raise ValueError(f"{where}: the step guard 'precondition' is "
                                     "configuration-only")
                if s.idempotent and key != "configuration":
                    raise ValueError(f"{where}: 'idempotent' is configuration-only")
                if s.capture and key == "rollback":
                    raise ValueError(f"{where}: rollback steps do not capture")
                if s.wait_until is not None and not (
                        key in ("preconditions", "verification")
                        or (key == "configuration" and s.driver == "tr069")):
                    raise ValueError(f"{where}: 'wait_until' is for preconditions, verification "
                                     "and tr069 configuration steps")
                if s.config_mode:
                    if key not in ("configuration", "rollback") or s.driver not in CLI_DRIVERS:
                        raise ValueError(f"{where}: 'config_mode' is for ssh/telnet "
                                         "configuration and rollback steps")
                    if not session.config_command:
                        raise ValueError(f"CONFIG_COMMAND_REQUIRED: step '{s.name}' sets "
                                         "config_mode but session.config_command is empty")
                if s.undoes is not None and s.undoes not in config_names:
                    raise ValueError(f"UNDOES_UNKNOWN_STEP: rollback step '{s.name}' undoes "
                                     f"'{s.undoes}', which is not a configuration step")

    def _validate_captures_and_secrets(self, phases) -> None:
        declared: set = set()
        total = 0
        for key, steps in phases:
            if key == "rollback":
                continue
            for s in steps:
                # A step reads only captures of EARLIER steps (preconditions ->
                # configuration -> verification); its own is extracted after it runs.
                texts = playbook_expr.strings(s.model_dump(exclude={"capture"})
                                              | {"capture": [c.model_dump(exclude={"key"})
                                                             for c in s.capture]})
                for ref in _refs(texts, _CAPTURE_REF):
                    if ref not in declared:
                        raise ValueError(f"CAPTURE_UNDECLARED: step '{s.name}' reads "
                                         f"{{{{capture.{ref}}}}} before it is captured")
                for c in s.capture:
                    if c.key in declared:
                        raise ValueError(f"capture key '{c.key}' is declared twice")
                    declared.add(c.key)
                total += len(s.capture)
        if total > MAX_CAPTURES_PER_PLAYBOOK:
            raise ValueError(f"at most {MAX_CAPTURES_PER_PLAYBOOK} captures per playbook")
        # Rollback and outputs may read any capture.
        late = [s.model_dump() for s in self.rollback] + [o.model_dump() for o in self.outputs]
        for ref in _refs(playbook_expr.strings(late), _CAPTURE_REF):
            if ref not in declared:
                raise ValueError(f"CAPTURE_UNDECLARED: {{{{capture.{ref}}}}} is never captured")

        secret_keys = [s.key for s in self.secrets]
        if len(secret_keys) != len(set(secret_keys)):
            raise ValueError("a secret key is declared twice")
        everything = [s.model_dump() for _, steps in phases for s in steps] + late
        for ref in _refs(playbook_expr.strings(everything), _SECRET_REF):
            if ref not in secret_keys:
                raise ValueError(f"SECRET_UNDECLARED: {{{{secret.{ref}}}}} is not declared "
                                 "in 'secrets'")

        if len(self.outputs) > MAX_OUTPUTS:
            raise ValueError(f"at most {MAX_OUTPUTS} outputs per playbook")
        output_keys = [o.key for o in self.outputs]
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("an output key is declared twice")

    def _validate_computed(self) -> None:
        if len(self.computed) > playbook_expr.MAX_ENTRIES:
            raise ValueError(
                f"COMPUTE_LIMIT: at most {playbook_expr.MAX_ENTRIES} computed entries"
            )
        earlier: set = set()
        for entry in self.computed:
            name = f"computed.{entry.key}"
            if name in earlier:
                raise ValueError(f"computed key '{entry.key}' is declared twice")
            for operand in playbook_expr.names(playbook_expr.parse(entry.expr)):
                if is_secret_name(operand):
                    raise ValueError(
                        f"COMPUTE_SECRET: computed '{entry.key}' reads secret-named '{operand}'"
                    )
                if operand.startswith("computed.") and operand not in earlier:
                    raise ValueError(
                        f"COMPUTE_NAME: computed '{entry.key}' may only read earlier "
                        f"computed keys, not '{operand}'"
                    )
            earlier.add(name)
        # every phase (templates, requests, step guards, validation and
        # threshold strings) and every output value
        dumped = [s.model_dump() for key in PHASE_KEYS.values() for s in getattr(self, key)]
        dumped += [o.model_dump() for o in self.outputs]
        used = []
        for text in playbook_expr.strings(dumped):
            for match in playbook_expr.TOKEN_SHAPE.finditer(text):
                body = match.group("body")
                if not _COMPUTED_HEAD.match(body):
                    continue
                token = _COMPUTED_TOKEN.match(body)
                if token is None:
                    raise ValueError(
                        f"COMPUTE_NAME: {{{{{body.strip()}}}}} is not a valid computed "
                        "reference; use {{computed.<key>}}"
                    )
                used.append(token.group(1))
        for key in dict.fromkeys(used):
            if f"computed.{key}" not in earlier:
                raise ValueError(
                    f"COMPUTE_NAME: {{{{computed.{key}}}}} is used but not declared in 'computed'"
                )


class PlaybookDefinitionOut(PlaybookDefinition):
    """PlaybookDefinition as the API returns it: v2 plus the read-only
    `steps` mirror (= configuration) so the pre-doc-48 editor still shows
    something. Removed in pe2 (doc 42 §11.1)."""
    steps: Optional[List[PlaybookStep]] = None

    @model_validator(mode="after")
    def mirror_steps(self) -> "PlaybookDefinitionOut":
        self.steps = list(self.configuration)
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
    definition: PlaybookDefinitionOut
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

    @field_validator("log")
    @classmethod
    def _mask_outputs(cls, log):
        return mask_log_outputs(log)
