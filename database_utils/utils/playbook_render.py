"""
The playbook language's render half: everything a render-only preview needs,
shared by the API and the worker (provisioning worker split, R1).

Moved verbatim from backend-erp `provisioning/executor.py` (the render
helpers and `dry_run_playbook`), `provisioning/redaction.py` and
`provisioning/guided.py`. The worker's executor imports these, so a dry run
(«Simular», the definition preview) and a live run render through ONE
implementation; backend-erp keeps its old module paths as re-export shims.

Pure: no DB, no drivers, no network. `re2` is imported inside
`_render_regex`, so a consumer that never renders a regex (auth-erp) needs no
re2 install.

Sections:
  * render     — `_render_step` and the unrendered-token guard, the rendered
                 `expect_*` / capture checks (`_prepare_checks`), the default
                 CLI target (`_render_target_item`).
  * captures   — the charset gate (`_gate_capture`), `merge_captures`.
  * manual     — the technician card (`_render_manual`, `is_secret_field`).
  * redaction  — literal secret scrubbing (`redact_secrets`, canon C14/C19).
  * guided     — the guided-run pause rule and step preview (doc 50 §4.1).
  * preview    — `dry_run_playbook` (doc 42 §9.1).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database_utils.models.isp import ProvisioningRunMode
from database_utils.schemas.playbook import (
    CLI_DRIVERS,
    MANUAL_DRIVER,
    PHASE_KEYS,
    SESSION_PROBE_STEP,
    job_steps,
    normalize_definition,
    output_secret_ref,
    playbook_warnings,
)
from database_utils.utils.acs_bootstrap import ACS_KEYS
from database_utils.utils.playbook_expr import is_secret_name
from database_utils.utils.playbook_template import (
    TOKEN_SHAPE,
    render_string,
    render_value,
    resolve_variables,
)
from database_utils.utils.provisioning_codes import Codes

# canon C15: dry_run_playbook's not-shipped list shrinks per phase. tr069
# shipped in Cycle 5 Phase 1 (provisioning/drivers/genieacs.py); ssh/telnet
# (+ping) shipped in Cycle 7 (doc 25 §4, provisioning/drivers/cli.py|ping.py)
# — removed here so the definition-preview endpoint stops flagging their steps.
_NOT_SHIPPED_DRIVERS = ("snmp",)

# Captured text may reach a later command: it must be unable to carry a
# newline, a quote or braces (doc 42 §4.3).
_CAPTURE_CHARSET = re.compile(r"[A-Za-z0-9_.:/+-]{0,128}")
_NOT_SIMULATED = "no evaluado en simulación"

GUIDED = ProvisioningRunMode.GUIDED.value
_PAUSING_PHASES = ("PRECONDITIONS", "CONFIGURATION", "VERIFICATION")
_PRECONDITIONS = "PRECONDITIONS"


class RenderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_RenderError = RenderError  # the executor's historical name


# --------------------------------------------------------------------- render

def _leftover_tokens(rendered: Any) -> List[str]:
    """Token-shaped text still present after substitution, at any depth.

    Uses the renderer's OWN recognizer (doc 34): the guard and the renderer
    must agree about what counts as token-shaped, or an unsubstituted
    placeholder reaches a device as literal configuration."""
    if isinstance(rendered, str):
        found = [m.group(0) for m in TOKEN_SHAPE.finditer(rendered)]
        # TOKEN_SHAPE excludes braces from a body, so a MALFORMED construct
        # matches nothing; any residual `{{` means a token that failed.
        if not found and "{{" in rendered:
            found = [rendered[m.start():m.start() + 40]
                     for m in re.finditer(r"\{\{", rendered)]
        return found
    if isinstance(rendered, dict):
        return [t for v in rendered.values() for t in _leftover_tokens(v)]
    if isinstance(rendered, list):
        return [t for v in rendered for t in _leftover_tokens(v)]
    return []


def _render_step(payload: dict, variables: Dict[str, Any], driver: Optional[str]) -> Tuple[Any, List[str]]:
    """`payload` is a step or a step guard; both carry `template`/`request`.
    Returns (rendered, problems): the renderer's missing variables plus any
    token-shaped leftovers, so a step never runs with a placeholder."""
    if payload.get("request") is not None and driver in ("http", "tr069"):
        rendered, missing = render_value(payload["request"], variables)
    else:
        rendered, missing = render_string(payload.get("template") or "", variables)
    already = set()
    for problem in missing:
        already.add(problem.strip())
        already.add(problem.split(" — ")[0].strip().strip("{} \t"))
    leftovers = [
        t for t in _leftover_tokens(rendered)
        if t.strip("{} \t") not in already and t not in already
    ]
    return rendered, list(dict.fromkeys(missing + leftovers))


def _render_text(text: Optional[str], variables: Dict[str, Any]) -> Optional[str]:
    if text is None:
        return None
    rendered, problems = render_string(str(text), variables)
    if problems:
        raise RenderError(Codes.RENDER_FAILED, f"template problems: {', '.join(problems)}")
    return rendered


def _render_regex(pattern: Optional[str], variables: Dict[str, Any]):
    """Render a regex with every token value ESCAPED (doc 42 §4.3), so a
    `{{cpe.serial}}` can never inject regex syntax; compile with RE2 (linear
    time, the worker holds every tenant's credentials)."""
    if pattern is None:
        return None
    problems: List[str] = []

    def _sub(match):
        value, missing = render_string(match.group(0), variables)
        problems.extend(missing)
        return re.escape(value)

    body = TOKEN_SHAPE.sub(_sub, pattern)
    if problems:
        raise RenderError(Codes.RENDER_FAILED, f"template problems: {', '.join(problems)}")
    import re2  # lazy: only a consumer that renders a regex needs google-re2

    try:
        return re2.compile(body)
    except Exception as exc:  # noqa: BLE001 — re2.error
        raise RenderError(Codes.REGEX_UNSUPPORTED, f"{Codes.REGEX_UNSUPPORTED}: {exc}") from None


def _threshold(value: Any, variables: Dict[str, Any]) -> Optional[str]:
    if value is None:
        return None
    return _render_text(str(value), variables)


def _prepare_checks(step: dict, variables: Dict[str, Any]) -> dict:
    """Everything a try evaluates after the driver, rendered BEFORE the driver
    call: a missing token or a bad regex fails at stage render, before
    anything is sent."""
    rules = step.get("validation") or {}
    captures = []
    for cap in step.get("capture") or []:
        rx = _render_regex(cap.get("regex"), variables)
        if rx.groups != 1:
            raise RenderError(Codes.REGEX_UNSUPPORTED,
                               f"capture '{cap.get('key')}': exactly one group required")
        spec = dict(cap, rx=rx, min=_threshold(cap.get("min"), variables),
                    max=_threshold(cap.get("max"), variables),
                    equals=_threshold(cap.get("equals"), variables))
        if (cap.get("type") or "text") == "number":
            for bound in ("min", "max"):
                if spec[bound] is not None:
                    try:
                        Decimal(spec[bound])
                    except InvalidOperation:
                        raise RenderError(Codes.RENDER_FAILED,
                                           f"capture '{cap.get('key')}' {bound} is not a number") from None
        captures.append(spec)
    return {
        "contains": _render_text(rules.get("expect_contains"), variables),
        "not_contains": _render_text(rules.get("expect_not_contains"), variables),
        "regex": _render_regex(rules.get("expect_regex"), variables),
        "not_regex": _render_regex(rules.get("expect_not_regex"), variables),
        # The UNRENDERED patterns, for error text: a rendered one embeds token
        # values regex-escaped, which literal redaction may not recognise.
        "regex_src": rules.get("expect_regex"),
        "not_regex_src": rules.get("expect_not_regex"),
        "status": rules.get("expect_status"),
        "captures": captures,
    }


def _render_target_item(payload: dict, variables: Dict[str, Any]) -> dict:
    """doc 25 §4.1 / doc 35 §4.4: the default CLI/ping target is the device the
    job is for, `{{device.item_id}}`; an explicit `target_item_id` wins. An
    unresolved target is left as-is and the driver reports
    TARGET_ITEM_REQUIRED."""
    target = payload.get("target_item_id")
    if target is None and "device.item_id" in variables:
        target = "{{device.item_id}}"
    if target is None:
        return payload
    rendered_target, _missing = render_string(str(target), variables)
    out = dict(payload)
    out["target_item_id"] = rendered_target
    return out


# --------------------------------------------------------------------- captures

def _gate_capture(value: Any, kind: str) -> Optional[str]:
    """The charset gate (doc 42 §4.3), applied at extraction and again when
    captures are merged into a later child. None = refused."""
    text = str(value)
    if kind == "number":
        try:
            Decimal(text)
        except InvalidOperation:
            return None
        return text if re.fullmatch(r"-?\d+(?:\.\d+)?", text) else None
    return text if _CAPTURE_CHARSET.fullmatch(text) else None


def _placeholders(definition: dict) -> Dict[str, str]:
    """Dry run (doc 42 §9.5): «capture.k» / «secret.k» / «acs.k» — no braces, so the
    leftover guard passes; nothing is generated or encrypted."""
    out = {}
    for key in PHASE_KEYS.values():
        for s in definition.get(key) or []:
            for cap in s.get("capture") or []:
                out[f"capture.{cap.get('key')}"] = f"«capture.{cap.get('key')}»"
    for spec in definition.get("secrets") or []:
        out[f"secret.{spec.get('key')}"] = f"«secret.{spec.get('key')}»"
    for key in ACS_KEYS:
        out[f"acs.{key}"] = f"«acs.{key}»"
    return out


def merge_captures(resolved: Dict[str, Any], captures: Optional[Dict[str, Any]],
                    definition: dict) -> Dict[str, str]:
    """Re-gate captures from earlier children / attempts before use (§6.3):
    a value that fails the gate is simply not provided, so a step reading it
    fails RENDER_FAILED."""
    kinds = {c.get("key"): c.get("type") or "text"
             for key in PHASE_KEYS.values() for s in definition.get(key) or []
             for c in s.get("capture") or []}
    kept = {}
    for key, value in (captures or {}).items():
        gated = _gate_capture(value, kinds.get(key, "text"))
        if gated is not None:
            resolved[f"capture.{key}"] = gated
            kept[key] = gated
    return kept


_merge_captures = merge_captures  # the executor's historical name


# --------------------------------------------------------------------- manual

def is_secret_field(field: dict) -> bool:
    """doc 42d §4.2: a manual field is secret when flagged OR when its value
    reads a secret-named token (secret.*, acs.inform_password, ...). The
    second test is the runtime twin of the save-time forcing, for a snapshot
    that bypassed PlaybookDefinition."""
    if field.get("secret"):
        return True
    for m in TOKEN_SHAPE.finditer(field.get("value") or ""):
        name = m.group("body").split("|", 1)[0].strip()
        if name.startswith("secret.") or is_secret_name(name):
            return True
    return False


def _render_manual(step: dict, variables: Dict[str, Any], secret_values: List[str], *,
                   dry_run: bool, notice: bool = False) -> Tuple[dict, List[str]]:
    """The technician card of a `driver: manual` step (doc 42d §6.1):
    (manual, problems). A secret field is rendered only to prove it resolves;
    its value is stored as None (the reveal re-renders it on demand). A dry
    run shows its token as a «placeholder». Every string is redacted."""
    spec = step.get("manual") or {}
    problems: List[str] = []

    def text(raw: Optional[str]) -> str:
        rendered, missing = render_string(raw or "", variables)
        problems.extend(missing or _leftover_tokens(rendered))
        return redact_secrets(rendered, secret_values)

    card = {"instructions": text(spec.get("instructions")), "fields": [], "checklist": []}
    if notice:  # a rollback notice: instructions only (§5.5)
        return card, problems
    for field in spec.get("fields") or []:
        value: Optional[str] = text(field.get("value"))
        secret = is_secret_field(field)
        if secret:
            value = f"«{(field.get('value') or '').strip('{} ')}»" if dry_run else None
        card["fields"].append({"key": field.get("key"), "label": field.get("label"),
                               "value": value, "copyable": field.get("copyable", True),
                               "secret": secret})
    card["checklist"] = [{"key": c.get("key"), "label": c.get("label")}
                         for c in spec.get("checklist") or []]
    return card, problems


# --------------------------------------------------------------------- redaction

def redact_secrets(value: Any, secret_values: Iterable[str]) -> Any:
    """Recursively replace every literal occurrence of any string in
    `secret_values` with '***' inside `value` (str / dict / list — anything
    else is returned unchanged). Empty/whitespace-only secrets are skipped so
    an unset credential can never turn into a blanket '***' replace of empty
    strings. Each secret's `re.escape` form is scrubbed too (longest first):
    a rendered regex embeds token values escaped (executor._render_regex), so
    a `-` in a token_urlsafe password reads `\\-` there."""
    secrets: List[str] = [s for s in secret_values if s and s.strip()]
    if not secrets:
        return value
    variants = {v for s in secrets for v in (s, re.escape(s))}
    return _redact(value, sorted(variants, key=len, reverse=True))


def _redact(value: Any, secrets: List[str]) -> Any:
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret in out:
                out = out.replace(secret, "***")
        return out
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    return value


# --------------------------------------------------------------------- guided

def should_pause(run_mode: Optional[str], dry_run: bool, phase: Optional[str],
                 step_index: int, approved_until: int, step: dict) -> bool:
    """True when step `step_index` must wait for «Continuar». A standalone
    job (`phase=None`) has no run, so it never pauses. The synthetic
    session probe only opens the CLI session (no command to approve)."""
    return (str(run_mode or "") == GUIDED and not dry_run
            and phase in _PAUSING_PHASES and step_index > approved_until
            and step.get("driver") != MANUAL_DRIVER
            and step.get("name") != SESSION_PROBE_STEP)


def render_preview(step: dict, variables: Dict[str, Any], secret_values: List[str]) -> dict:
    """{name, label, rendered}: the step rendered exactly as it would run
    (variables, captures, secrets), then masked like a step log entry. A
    template problem is not reported here: the approved step fails
    RENDER_FAILED when it runs."""
    if step.get("request") is not None and step.get("driver") in ("http", "tr069"):
        rendered, _ = render_value(step["request"], variables)
    else:
        rendered, _ = render_string(step.get("template") or "", variables)
    rendered = redact_secrets(rendered, secret_values)
    if isinstance(rendered, str):
        rendered = rendered[:5_000]
    return {"name": step.get("name"), "label": step.get("label") or step.get("name"),
            "rendered": rendered}


def is_pause_entry(entry: Any) -> bool:
    """The PENDING log entry a guided park leaves for the step it waits on."""
    return (isinstance(entry, dict) and entry.get("status") == "PENDING"
            and (entry.get("detail") or {}).get("kind") == "guided")


def without_pause(log: Optional[dict]) -> dict:
    """`log` minus the guided pause (entry, `pending_kind`, `guided_preview`).
    The step was never sent: left in the log, models-utils ran_steps would
    count it (status PENDING) and a rollback would undo a step that never ran."""
    log = {k: v for k, v in (log or {}).items() if k not in ("pending_kind", "guided_preview")}
    log["steps"] = [e for e in log.get("steps") or [] if not is_pause_entry(e)]
    return log


# --------------------------------------------------------------------- preview

def _shows_probe(definition: dict) -> bool:
    if not any(s.get("driver") in CLI_DRIVERS for key in ("configuration", "verification")
               for s in definition.get(key) or []):
        return False
    pre = definition.get("preconditions") or []
    return not (pre and pre[0].get("driver") in CLI_DRIVERS)


def dry_run_playbook(definition: dict, variables: Optional[Dict[str, Any]], *,
                     category_tier: Optional[str] = None,
                     purpose: Optional[str] = None) -> dict:
    """Render-only preview grouped by phase, plus outputs and warnings (doc 42
    §9.1). No drivers, no DB. Does NOT stamp `last_dry_run_version`; only a
    dry-run job or run does (canon C7, doc 42 §9.2). `category_tier` /
    `purpose` (a binding of the playbook) enable CPE_NETWORK_PRECONDITION."""
    definition = normalize_definition(dict(definition or {})) or {}
    resolved, missing_required = resolve_variables(definition, variables)
    resolved.update(_placeholders(definition))
    steps_report: List[dict] = []
    all_missing: List[str] = list(missing_required)
    cfg_names = {s.get("name") for s in definition.get("configuration") or []}

    groups = []
    if _shows_probe(definition):
        groups.append(("preconditions", [job_steps(definition, _PRECONDITIONS, probe=True)[0]]))
    groups += [(key, definition.get(key) or []) for key in PHASE_KEYS.values()]
    for group, steps in groups:
        for step in steps:
            rendered, missing = _render_step(step, resolved, step.get("driver"))
            issues: List[str] = []
            if missing:
                issues.append(f"template problems: {', '.join(missing)}")
                all_missing.extend(missing)
            try:
                checks = _prepare_checks(step, resolved)
            except RenderError as exc:
                checks = None
                issues.append(str(exc))
            if step.get("driver") in _NOT_SHIPPED_DRIVERS:
                issues.append(
                    f"driver '{step['driver']}' is not shipped yet — this step will "
                    f"fail at execution time (use 'http', 'tr069', 'ssh', 'telnet', "
                    f"'ping', or 'simulator')"
                )
            if step.get("undoes") and step["undoes"] not in cfg_names:
                issues.append(f"undoes '{step['undoes']}', which is not a configuration step")
            row = {"group": group, "name": step.get("name"),
                   "label": step.get("label") or step.get("name"),
                   "driver": step.get("driver"), "rendered": rendered, "issues": issues}
            if step.get("driver") == MANUAL_DRIVER:
                # doc 42d §6.1: the Simular card preview; a token that would
                # fail RENDER_FAILED at park is caught here.
                row["manual"], problems = _render_manual(
                    step, resolved, [], dry_run=True, notice=group == "rollback")
                if problems:
                    issues.append(f"template problems: {', '.join(dict.fromkeys(problems))}")
                    all_missing.extend(problems)
            if checks and checks["captures"]:
                row["display"] = _NOT_SIMULATED
            if step.get("name") == SESSION_PROBE_STEP:
                row["implicit"] = True
            steps_report.append(row)

    for spec in definition.get("outputs") or []:
        issues = []
        if output_secret_ref(spec.get("value") or "") is not None:
            rendered = f"«{output_secret_ref(spec['value'])}»"
        else:
            rendered, missing = render_string(spec.get("value") or "", resolved)
            if missing:
                issues.append(f"template problems: {', '.join(missing)}")
                all_missing.extend(missing)
        steps_report.append({"group": "outputs", "name": spec.get("key"),
                             "label": spec.get("label"), "driver": None,
                             "rendered": rendered, "issues": issues})

    return {
        "valid": not any(s["issues"] for s in steps_report) and not missing_required,
        "missing_variables": list(dict.fromkeys(all_missing)),
        "steps": steps_report,
        "warnings": playbook_warnings(definition, category_tier=category_tier,
                                      purpose=purpose),
    }
