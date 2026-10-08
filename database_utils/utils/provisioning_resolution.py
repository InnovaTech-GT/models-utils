# utils/provisioning_resolution.py
"""
Provisioning resolution (Cycle 10, doc 35 §3.2).

Lives in models-utils rather than backend-erp because the workflow engine's
ENQUEUE_PROVISIONING path calls it directly: the engine (models-utils) cannot
import backend-erp, and backend-erp already imports models-utils, so moving
resolution down the stack is the only import direction that compiles.

WHAT CHANGED IN CYCLE 10. Resolution used to start from a `Topology` — a named,
ordered chain of device TYPES — and match each chain position against the
client's assigned inventory. It now starts from the service's CPE and walks the
company's network graph to the root (utils/network_graph.resolve_path). The
difference is not cosmetic:

- Every node on the path IS a concrete device, so there is nothing left to
  match. MISSING_DEVICE, AMBIGUOUS_DEVICE and PINNED_DEVICE_UNAVAILABLE — the
  three most common provisioning failures in the old system — cannot occur.
- Playbooks bind to device types (overridable per node) instead of to a chain,
  so ONE run executes SEVERAL playbooks, one per configured device.
- Path length is variable, so positional variables are gone (see below).

Ordering is LEAF -> ROOT everywhere (doc 35 §3.1): devices are configured from
the subscriber outward, CPE first and core last, for every purpose, in the
executor and in the UI alike.

THE VARIABLE NAMESPACE (doc 35 §4). Positional namespaces are RETIRED with no
compatibility shim: `chain[n]`, `edge_devices[n]`, `core_devices[n]` and the
`position` attribute are gone, and ng2_topology_drop refuses to run over any
playbook that still contains them.

  device.<attr>              the device THIS playbook is running on
  cpe.<attr>                 the subscriber edge device that triggered the run
  path.<category_key>.<attr> any other node on THIS RUN's path, named by its
                             device-category role; nearest-to-the-CPE wins if a
                             role repeats
  path.<path_role>.<attr>    a node whose device type carries a per-company
                             path role ("mufa_principal"); emitted only when
                             exactly one node on the path holds it (doc 40)
  computed.<key>             a playbook's declared integer arithmetic, evaluated
                             here (refusal) and again by the renderer (doc 40)
  service_plan.<field|param> plan fields + the plan's tenant-authored rows
  client.<attr>              built-in subscriber fields + the tenant's own
                             custom client attributes (built-ins win a clash)
  service.<attr>             the client_service itself
  input.<key>                author-declared playbook variables (namespace
                             applied at REFERENCE time; the declared key stays
                             bare)

Addressing is by CATEGORY, not device-type slug and not relative hop. Category
is the stable semantic ROLE ("OLT") on a curated, platform-global table with a
unique immutable key; a device-type slug is the hardware ("Huawei MA5800") and
would break every playbook on a vendor swap. Relative hops break the instant a
splitter is inserted, and have no downward form — a core-router playbook needs
the OLT and CPE BELOW it, which is why addressing is path-relative rather than
upstream-relative.

`variables` remains a FLAT dict — the KEYS are the full dotted strings.
`path.olt.serial` is a key, not a walk. There is no nested structure, which
keeps the renderer a pure dictionary lookup (ADR-006: no expressions, no
attribute access). A nested dict under a namespace prefix deliberately does NOT
satisfy a dotted token; allowing it would be attribute access by the back door.

Two dicts come out, not one: `shared_variables` is identical for every node in
the run, while `device.*` differs per node. Each child job's `variables` column
is written as `shared | device_variables[item_id]`, so the executor and the
renderer still receive exactly one flat dict and their contract is untouched.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    ClientService,
    DeviceTypePlaybook,
    InventoryItem,
    InventoryItemPlaybook,
    InventoryItemPort,
    NetworkLink,
    Playbook,
    ProvisioningJobStatus,
    ProvisioningRun,
)
from database_utils.schemas.playbook import PHASE_KEYS, normalize_definition
from database_utils.utils import playbook_expr
from database_utils.utils.network_graph import GraphError, resolve_path


class ResolutionError(Exception):
    """Raised when a client service's provisioning cannot be resolved.

    `errors` is a list of {code, position?, item_id?, device_type_id?,
    device_type_name?, category?} dicts — collected across EVERY node on the
    resolved path so the caller (technician) sees the whole shopping list, not
    one error per retry.
    """

    def __init__(self, code: str, detail: str, errors: Optional[List[Dict[str, Any]]] = None):
        self.code = code
        self.detail = detail
        self.errors = errors or [{"code": code, "detail": detail}]
        super().__init__(detail)


@dataclass
class ResolvedNode:
    """One node on a service's configuration path.

    `position` is the hop count from the CPE (the CPE itself is 0) — a FACT
    about the resolved path, never an addressing mechanism. Nothing templates
    it; it exists so the UI can render the path in order and so `depth` has a
    value.
    """

    position: int
    item_id: Any
    serial_number: Optional[str]
    mac_address: Optional[str]
    device_type_id: Any
    device_type_name: str
    category_key: Optional[str]
    category_tier: Optional[str]
    mgmt_host: Optional[str]
    mgmt_port: Optional[int]
    is_passive: bool = False
    playbook_id: Any = None
    # "node" (an inventory_item_playbook override), "device_type" (the type's
    # default), or None (nothing bound).
    playbook_source: Optional[str] = None
    # --- port-level topology (doc 40 §3.3.1) ---------------------------------
    label: Optional[str] = None
    path_role: Optional[str] = None
    # The port on THIS node that the next node toward the CPE hangs off, read
    # from that node's network_link. None = unknown (no link, or a stale one):
    # the frame then omits the key rather than emitting "" (see PORT_ATTRIBUTES).
    out_slot: Optional[int] = None
    out_port: Optional[int] = None
    out_port_name: Optional[str] = None
    # playbook.version at resolution; the worker refuses a child whose playbook
    # was edited since (PLAYBOOK_CHANGED_DURING_RUN, backend-erp).
    playbook_version: Optional[int] = None


@dataclass
class ResolvedProvisioning:
    """The full result of resolving one run.

    `path` is every node including passives — the UI shows the whole path so an
    operator can see that a splitter was considered and deliberately skipped,
    rather than wondering where it went. `steps` is the subset that will
    actually be configured.
    """

    path: List[ResolvedNode] = field(default_factory=list)
    steps: List[ResolvedNode] = field(default_factory=list)
    shared_variables: Dict[str, Any] = field(default_factory=dict)
    device_variables: Dict[Any, Dict[str, Any]] = field(default_factory=dict)
    # {role: [item_id, ...]} for path roles held by more than one node on this
    # path. Such a role gets no path.<role>.* frame (doc 40 §3.3.1).
    ambiguous_roles: Dict[str, List[str]] = field(default_factory=dict)


INPUT_NAMESPACE = "input"


def input_key(key: str) -> str:
    """The token an author-declared variable is referenced by (doc 33).

    The declaration keeps its bare snake_case `key` — the authoring form and
    its validator are untouched — and the namespace is applied at REFERENCE
    time. Lives here, not in backend-erp's renderer, because the workflow
    engine also produces author variables and models-utils cannot import
    backend-erp (import direction is strictly downward)."""
    return key if key.startswith(f"{INPUT_NAMESPACE}.") else f"{INPUT_NAMESPACE}.{key}"


SCOPE_PLAN = "plan"
SCOPE_SERVICE = "service"


def iter_provisioning_params(params: Any):
    """Yield (key, value, scope) from a provisioning_params column.

    The stored shape is a LIST of {"key", "value", "description", "scope"} rows
    so the UI can carry a human explanation per parameter and mark which ones
    are valued per service. The pre-namespace shape was a bare {"vlan": 110}
    dict; the `pv1` revision converts every row, but this reader stays tolerant
    of both so a hand-written dict (or an xlsx import) never explodes at
    provisioning time. A row with no `scope` is plan-scoped, which is what
    every row written before this feature is."""
    if not params:
        return
    if isinstance(params, dict):
        for key, value in params.items():
            yield str(key), value, SCOPE_PLAN
        return
    if isinstance(params, list):
        for row in params:
            if isinstance(row, dict) and row.get("key"):
                scope = row.get("scope") or SCOPE_PLAN
                yield str(row["key"]), row.get("value"), str(scope)


def _service_param_values(client_service: Any) -> Dict[str, Any]:
    """The per-service VALUES, keyed. The service supplies only values — the
    plan owns the declaration — so anything here that the plan does not declare
    is ignored rather than emitted as a stray variable."""
    values: Dict[str, Any] = {}
    for key, value, _scope in iter_provisioning_params(
        getattr(client_service, "provisioning_params", None)
    ):
        values[key] = value
    return values


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# A namespace segment the renderer can actually match. `field_key` is
# validated as alnum+underscore and lowercased, which still permits a leading
# digit ("5g_profile") — that would produce a token no one can reference, so
# such keys are skipped rather than emitted as dead weight.
_REFERENCEABLE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _coerce_custom_value(value: Optional[str], field_type: Any) -> Any:
    """Custom field values are all stored as strings; give NUMBER/BOOLEAN their
    natural type so a template renders `100` rather than `100.0`, and so a
    boolean reads as true/false instead of the literal string."""
    if value is None:
        return ""
    kind = getattr(field_type, "value", field_type)
    kind = str(kind).upper() if kind is not None else ""
    if kind == "NUMBER":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number
    if kind == "BOOLEAN":
        return str(value).strip().lower() in ("true", "1", "yes", "y")
    return value


def iter_client_custom_fields(client: Any):
    """Yield (field_key, typed value) for a client's custom attributes.

    Reads the `client_custom_field_value` -> `custom_field_definition` join the
    Client model already exposes as `custom_field_values`. Degrades to nothing
    when the relationship is absent (detached/partial objects, tests) — a
    missing attribute must never crash a provisioning job."""
    for row in getattr(client, "custom_field_values", None) or []:
        definition = getattr(row, "field_definition", None)
        key = getattr(definition, "field_key", None)
        if not key or not _REFERENCEABLE_KEY.match(key):
            continue
        yield key, _coerce_custom_value(getattr(row, "value", None),
                                        getattr(definition, "field_type", None))


def resolve_playbook_for(
    db: Session, item: InventoryItem, purpose: str
) -> tuple[Any, Optional[str]]:
    """(playbook_id, source) for one node and one purpose.

    Precedence is node override -> device-type default -> none (doc 35 §2.4).
    A single module-level helper so the resolver, the API's path preview and
    the node detail endpoint share one lookup semantics rather than three that
    could drift.
    """
    override = db.execute(
        sa.select(InventoryItemPlaybook.playbook_id).where(
            InventoryItemPlaybook.inventory_item_id == item.id,
            InventoryItemPlaybook.purpose == purpose,
        )
    ).scalar_one_or_none()
    if override is not None:
        return override, "node"

    default = db.execute(
        sa.select(DeviceTypePlaybook.playbook_id).where(
            DeviceTypePlaybook.device_type_id == item.device_type_id,
            DeviceTypePlaybook.purpose == purpose,
        )
    ).scalar_one_or_none()
    if default is not None:
        return default, "device_type"

    return None, None


# Amendment 4: a device-derived variable is any token in the three device
# namespaces.
#
# The optional `| filter ...` suffix (doc 34) must be tolerated here, or a token
# carrying a filter reads as NOT device-derived and amendment 4 silently
# downgrades a resolution error from fatal.
#
# BOTH patterns in this module FAIL OPEN. A namespace that is emitted but not
# listed here does not raise, does not warn, and does not fail a test that is
# not looking for it — it quietly turns a hard resolution error into a partial
# run that half-configures a paying customer. If you add a namespace, add it
# here in the same commit. test_device_variable_pattern_matches_the_new_
# namespaces exists solely to catch that omission.
_FILTER_SUFFIX = r'(?:\s*\|[^{}\n]*)?'

_DEVICE_VARIABLE_PATTERN = re.compile(
    r'\{\{\s*(?:device|cpe|path\.[a-z][a-z0-9_]*)\.[a-z][a-z0-9_]*'
    + _FILTER_SUFFIX + r'\s*\}\}'
)

# Attributes emitted for every device, in all three device namespaces. Keep in
# sync with the editor catalog in frontend-erp (lib/playbookVariables.ts) — the
# editor is the only place an operator discovers these.
DEVICE_ATTRIBUTES = (
    "item_id", "serial", "mac", "type", "category", "category_tier",
    "mgmt_host", "mgmt_port", "depth",
)

# Port attributes (doc 40 §3.3.1), emitted per device only when the port is
# recorded, so a frame's keys are DEVICE_ATTRIBUTES plus a subset of these.
# Mirrored with DEVICE_ATTRIBUTES in frontend-erp lib/playbookGrammar.ts. All
# single segments, so _DEVICE_VARIABLE_PATTERN already matches them.
PORT_ATTRIBUTES = ("out_slot", "out_port", "out_port_name")


def build_device_frame(node: ResolvedNode, prefix: str) -> Dict[str, Any]:
    """Flat dotted keys for one device under one namespace prefix.

    Keys are the whole dotted string by design (see the module docstring): a
    nested dict under `path` would be attribute access by the back door.
    """
    return {
        f"{prefix}.item_id": str(node.item_id),
        f"{prefix}.serial": node.serial_number or "",
        f"{prefix}.mac": node.mac_address or "",
        f"{prefix}.type": node.device_type_name or "",
        f"{prefix}.category": (node.category_key or "").lower(),
        f"{prefix}.category_tier": node.category_tier or "",
        f"{prefix}.mgmt_host": node.mgmt_host or "",
        f"{prefix}.mgmt_port": str(node.mgmt_port) if node.mgmt_port else "",
        f"{prefix}.depth": node.position,
    } | {
        # Emitted only when known (doc 40 §3.3.1). An absent key is NOT "":
        # the renderer tests presence as `vars[name] is not None`, so an empty
        # string would render `slot  link` and fail open.
        f"{prefix}.{attr}": getattr(node, attr)
        for attr in PORT_ATTRIBUTES
        if getattr(node, attr) is not None
    }

_DEVICE_NAMESPACES = ("device", "cpe", "path")


def _computed_names(definition: Any) -> Optional[set]:
    """Every operand name read by the definition's `computed` block, or None
    when the block cannot be parsed. Callers treat None as "references
    everything" — the fail-open posture of this module's two regexes."""
    try:
        names: set = set()
        for entry in (definition or {}).get("computed") or []:
            names.update(playbook_expr.names(playbook_expr.parse(entry["expr"])))
        return names
    except Exception:  # noqa: BLE001 — any malformed shape is "unknown"
        return None


def _playbook_references_token(playbook: Playbook, token: str) -> bool:
    """Whether the playbook's own definition templates this exact token, or
    reads it as a `computed` operand.

    Used to decide whether a missing per-service parameter is fatal: declaring
    `pppoe_user` per-service must not block a SUSPENSION playbook that never
    reads it. Same posture as `_playbook_references_device_variables` — fail
    safe (treat as referenced) when the definition cannot be introspected,
    including a `computed` block that does not parse (doc 40 §3.3.2)."""
    try:
        blob = json.dumps(playbook.definition)
    except (TypeError, ValueError):
        return True
    if re.search(
        r"\{\{\s*" + re.escape(token) + _FILTER_SUFFIX + r"\s*\}\}", blob
    ) is not None:
        return True
    names = _computed_names(playbook.definition)
    return names is None or token in names


def _playbook_references_device_variables(playbook: Playbook) -> bool:
    """Amendment 4 (doc 20 normative amendments #4, appendix workflow-
    provisioning §3 option (c)): for non-ACTIVATION purposes, a device-chain
    resolution error is fatal only when the playbook's own template actually
    reads a device-derived variable. A suspend/reactivate/deprovision
    playbook that never templates a device variable (e.g. it only flips a
    VLAN via a stored integration) must not fail just because the chain is
    ambiguous or missing equipment — e.g. DEPROVISION fired after the tech
    already recovered the CPE, or SUSPENSION with two client-assigned CPEs
    and none service-bound."""
    try:
        blob = json.dumps(playbook.definition)
    except (TypeError, ValueError):
        return True  # cannot introspect -> fail safe, treat as device-referencing
    if _DEVICE_VARIABLE_PATTERN.search(blob):
        return True
    # A computed operand reads a device as surely as a template does (doc 40
    # §3.3.2 fail-open fix); an unparseable block counts as referencing.
    names = _computed_names(playbook.definition)
    return names is None or any(
        n.split(".", 1)[0] in _DEVICE_NAMESPACES for n in names
    )


def _node_from_item(db: Session, item: InventoryItem, position: int,
                    purpose: str) -> ResolvedNode:
    device_type = item.device_type
    category = getattr(device_type, "category_ref", None) if device_type else None
    node = ResolvedNode(
        position=position,
        item_id=item.id,
        serial_number=item.serial_number,
        mac_address=item.mac_address,
        device_type_id=item.device_type_id,
        device_type_name=getattr(device_type, "name", "") or "",
        category_key=getattr(category, "key", None),
        category_tier=getattr(category, "tier", None),
        mgmt_host=item.mgmt_host,
        mgmt_port=item.mgmt_port,
        is_passive=bool(getattr(category, "is_passive", False)),
        label=getattr(item, "label", None),
        path_role=getattr(device_type, "path_role", None),
    )
    if not node.is_passive:
        node.playbook_id, node.playbook_source = resolve_playbook_for(db, item, purpose)
    return node


def _attach_ports(db: Session, path: List[ResolvedNode], company_id: Any) -> None:
    """Fill out_* on every node from the links of the node below it.

    One company-scoped query for the whole path, joined to the upstream ports.
    path[i].out_* is the port on path[i] that path[i-1] hangs off — but ONLY if
    that link's up_item_id is path[i]: the path (parent_id) and the links are
    read in separate statements, and a reparent committed in between must not
    hand this node another device's port (doc 40 §5 DI-11). The CPE (i = 0)
    has nothing below it and never gets out_*.
    """
    rows = db.execute(
        sa.select(NetworkLink.down_item_id, NetworkLink.up_item_id,
                  InventoryItemPort.slot, InventoryItemPort.number,
                  InventoryItemPort.name)
        .join(InventoryItemPort, InventoryItemPort.id == NetworkLink.up_port_id)
        .where(
            NetworkLink.company_id == company_id,
            NetworkLink.down_item_id.in_([n.item_id for n in path]),
        )
    ).all()
    by_down = {row.down_item_id: row for row in rows}
    for below, node in zip(path, path[1:]):
        link = by_down.get(below.item_id)
        if link is None or link.up_item_id != node.item_id:
            continue
        node.out_slot, node.out_port, node.out_port_name = link.slot, link.number, link.name


# --- resolution-time refusal (doc 40 §3.3.2) --------------------------------

# Namespaces the resolver fills. `input.*` is the backend renderer's (declared
# variables with their own required/default rule); bare legacy tokens are not
# ours either.
RESOLVER_NAMESPACES = frozenset(playbook_expr.NAMESPACES)

# The renderer's _VAR_HEAD (name, optional [i] index after the first segment),
# followed by a filter pipe or the end of the body.
_TOKEN_HEAD = re.compile(
    r"[ \t]*([a-z][a-z0-9_]*(?:\[\d{1,3}\])?(?:\.[a-z][a-z0-9_]*)*)[ \t]*(?:\||$)"
)
# A regex, not a substring test: `| default:` is the filter, `defaults` in a
# literal argument is not (doc 40 §5 PS-3).
_DEFAULT_FILTER = re.compile(r"\|\s*default\s*:")
# Quoted filter arguments are literals: `replace:"|default:","x"` must not read
# as a default filter, or the up-front refusal would skip a token that then
# fails mid-run (security review F2).
_QUOTED_ARG = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def _has_default_filter(body: str) -> bool:
    return bool(_DEFAULT_FILTER.search(_QUOTED_ARG.sub("", body)))


def _step_tokens(definition: Any) -> tuple:
    """(tokens, malformed) for every string the executor renders: in EVERY
    phase, rollback included — a run must not start if its undo cannot render
    (doc 42 §9.4) — templates, http/tr069 requests, target_item_id, step
    guards, validation strings and capture regexes/thresholds, plus every
    output value. capture.* and secret.* are outside RESOLVER_NAMESPACES, so
    the caller skips them; their correctness is checked at save time.

    tokens is (name, has_default) per parseable token. malformed is every
    construct the renderer leaves in place — a token-shaped body whose head
    does not parse, or a residual `{{` outside any token shape — which the
    executor's leftover guard fails the step on, whatever its namespace."""
    try:
        d = normalize_definition(definition or {})
    except ValueError:  # LEGACY_STEPS_CONFLICT on a stored row: scan it raw
        d = definition or {}
    if not isinstance(d, dict):
        d = {}
    fields = []
    for key in PHASE_KEYS.values():
        for step in d.get(key) or []:
            if not isinstance(step, dict):
                continue
            fields += [step.get("template"), step.get("request"), step.get("target_item_id"),
                       step.get("validation"), step.get("capture"), step.get("precondition")]
    fields += [o.get("value") for o in d.get("outputs") or [] if isinstance(o, dict)]
    tokens, malformed = [], []
    for text in playbook_expr.strings(fields):
        for match in playbook_expr.TOKEN_SHAPE.finditer(text):
            body = match.group("body")
            head = _TOKEN_HEAD.match(body)
            if head:
                tokens.append((head.group(1), _has_default_filter(body)))
            else:
                malformed.append(match.group(0))
        rest = playbook_expr.TOKEN_SHAPE.sub("", text)
        malformed += [rest[m.start():m.start() + 40] for m in re.finditer(r"\{\{", rest)]
    return tokens, malformed


def _is_port_token(name: str) -> bool:
    parts = name.split(".")
    return parts[-1] in PORT_ATTRIBUTES and parts[0] in ("device", "path")


class _Explainer:
    """Turns "token X has no value on node n" into one of the three doc 40
    §3.3.2 errors, naming the device and the reason."""

    def __init__(self, path_nodes: Dict[str, ResolvedNode],
                 ambiguous_roles: Dict[str, List[str]]):
        self.path_nodes = path_nodes  # path segment (role or category) -> node
        self.ambiguous_roles = ambiguous_roles

    def __call__(self, name: str, step: ResolvedNode) -> Dict[str, Any]:
        parts = name.split(".")
        node = None
        if parts[0] == "path" and len(parts) == 3:
            segment = parts[1]
            if segment in self.ambiguous_roles:
                return {
                    "code": "ROLE_AMBIGUOUS", "token": name, "role": segment,
                    "item_ids": self.ambiguous_roles[segment],
                    "detail": (f"'{name}': more than one device on this path "
                               f"has the role '{segment}'"),
                }
            node = self.path_nodes.get(segment)
            if node is None:
                return {
                    "code": "UNRESOLVED_TOKEN", "token": name, "reason": "not_on_path",
                    "detail": f"'{name}': no device on this path is a '{segment}'",
                }
        elif parts[0] == "device" and len(parts) == 2:
            node = step
        if node is not None and node.position > 0 and parts[-1] in PORT_ATTRIBUTES:
            reason = "no_slot" if node.out_port is not None else "no_link"
            return {
                "code": "PORT_NOT_RECORDED", "token": name,
                "item_id": str(node.item_id),
                "label": node.label or node.serial_number or node.device_type_name,
                "position": node.position, "reason": reason,
                "detail": (
                    f"'{name}': the port on "
                    f"'{node.label or node.serial_number or node.device_type_name}' "
                    + ("has no slot" if reason == "no_slot"
                       else "that the next device connects to is not recorded")
                ),
            }
        return {
            "code": "UNRESOLVED_TOKEN", "token": name, "reason": "missing_value",
            "detail": f"'{name}' has no value for this service",
        }


def _last_activation_frames(db: Session, client_service: Any) -> Optional[Dict[str, Any]]:
    """Frames of the service's last SUCCEEDED, non-dry ACTIVATION run — the
    port facts the devices were actually configured with."""
    return db.execute(
        sa.select(ProvisioningRun.frames)
        .where(
            ProvisioningRun.company_id == client_service.company_id,
            ProvisioningRun.client_service_id == client_service.id,
            ProvisioningRun.purpose == PURPOSE_ACTIVATION,
            ProvisioningRun.dry_run.is_(False),
            ProvisioningRun.status == ProvisioningJobStatus.SUCCEEDED,
        )
        .order_by(ProvisioningRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _port_fact_drift(baseline: Dict[str, Any], step: ResolvedNode,
                     names: Iterable[str], variables: Dict[str, Any],
                     path_nodes: Dict[str, ResolvedNode]) -> List[Dict[str, Any]]:
    """PATH_CHANGED_SINCE_ACTIVATION for each port fact this step reads whose
    value differs from the baseline frames (doc 40 §3.3.2, §5 PS-1).

    A key absent from the baseline means the activation never knew that port
    (activated before this feature, or the port was not recorded yet), which
    is "no baseline" for that fact, not drift. A fact cleared since activation
    (now None) IS drift: the `| default` skip must not let it render a guess.
    """
    errors = []
    for name in names:
        if not _is_port_token(name):
            continue
        if name.startswith("device."):
            owner = step
            frame = ((baseline.get("device") or {}).get(str(step.item_id)) or {})
        else:
            owner = path_nodes.get(name.split(".")[1])
            frame = baseline.get("shared") or {}
        was, now = frame.get(name), variables.get(name)
        if was is not None and was != now:
            errors.append({
                "code": "PATH_CHANGED_SINCE_ACTIVATION", "token": name,
                "was": was, "now": now,
                "item_id": str(owner.item_id) if owner is not None else None,
                "detail": (f"'{name}' was {was!r} when this service was activated "
                           f"and is {now!r} now; re-activate it first"),
            })
    return errors


def _refusals(db: Session, client_service: Any, purpose: str,
              steps: List[ResolvedNode], playbooks: Dict[Any, Playbook],
              shared: Dict[str, Any], device_variables: Dict[Any, Dict[str, Any]],
              path_nodes: Dict[str, ResolvedNode],
              ambiguous_roles: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Every error that would otherwise surface mid-run, after earlier devices
    were already configured (doc 40 §3.3.2). Runs go leaf -> root on frozen
    frames, so a missing OLT port would fail the OLT step after the ONU step
    ran; refusing here means nothing touches a device first."""
    explain = _Explainer(path_nodes, ambiguous_roles)
    errors: List[Dict[str, Any]] = []
    baseline = (_last_activation_frames(db, client_service)
                if purpose != PURPOSE_ACTIVATION else None)
    for node in steps:
        definition = playbooks[node.playbook_id].definition or {}
        variables = dict(shared) | device_variables.get(node.item_id, {})
        computed = definition.get("computed") or []
        if not isinstance(computed, list) or not all(isinstance(e, dict) for e in computed):
            errors.append({"code": "COMPUTE_SYNTAX", "item_id": str(node.item_id),
                           "detail": "the playbook's 'computed' block is malformed"})
            computed = []
        values, missing, compute_errors = playbook_expr.evaluate_all(computed, variables)
        errors += [err | {"item_id": str(node.item_id)} for err in compute_errors]
        errors += [explain(name, node) for name in missing]
        variables |= values
        declared = {f"computed.{e.get('key')}" for e in computed}

        tokens, malformed = _step_tokens(definition)
        errors += [{
            "code": "UNRESOLVED_TOKEN", "token": raw, "reason": "malformed",
            "item_id": str(node.item_id),
            "detail": f"'{raw}' is not a valid token; the step would fail on it",
        } for raw in malformed]
        for name, has_default in tokens:
            if has_default or name.split(".", 1)[0] not in RESOLVER_NAMESPACES:
                continue
            if name in declared and name not in values:
                continue  # its own failure is already reported above
            if variables.get(name) is None:
                errors.append(explain(name, node))

        if baseline:
            read = [name for name, _ in tokens] + list(_computed_names(definition) or ())
            errors += _port_fact_drift(baseline, node, dict.fromkeys(read),
                                       variables, path_nodes)

    unique: Dict[str, Dict[str, Any]] = {}
    for err in errors:  # one path.* token read by two playbooks is one problem
        unique.setdefault(json.dumps(err, sort_keys=True, default=str), err)
    return list(unique.values())


def resolve_provisioning(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
) -> ResolvedProvisioning:
    """Resolve a service's configuration path, its per-node playbooks and its
    variable frames — or raise ResolutionError.

    Algorithm (doc 35 §3.2, doc 40 §3.3):

    1. client_service.cpe_item_id unset            -> CPE_NOT_SET
    2. that CPE not attached to the graph          -> CPE_NOT_ATTACHED
    3. path = resolve_path(cpe), ordered LEAF -> ROOT
    4. a node whose category is_passive contributes nothing but stays on `path`
    5. every other node resolves node override -> device-type default -> none
    6. an ACTIVE node with no playbook for `purpose` is reported
       PLAYBOOK_NOT_BOUND — fatal for ACTIVATION, non-fatal otherwise
    7. every step playbook's `computed` block and every resolver-owned token it
       renders must have a value, and (other than for ACTIVATION) every port
       fact it reads must match the last live activation — else
       RESOLUTION_FAILED with the whole list, before any device is touched

    Step 6 preserves the pre-existing fatality posture exactly: ACTIVATION
    fails visibly (a half-provisioned install is worse than a refused one),
    while a SUSPENSION whose OLT happens to have no suspend playbook still
    suspends whatever it can.
    """
    cpe_id = getattr(client_service, "cpe_item_id", None)
    if cpe_id is None:
        raise ResolutionError(
            "CPE_NOT_SET",
            "This service has no CPE assigned, so it has no place in the network",
        )

    try:
        path_items = resolve_path(db, cpe_id, client_service.company_id)
    except GraphError as exc:
        raise ResolutionError(exc.code, exc.detail) from exc

    if not path_items:
        raise ResolutionError(
            "CPE_NOT_ATTACHED",
            "This service's CPE is not attached to the network graph",
        )
    if not path_items[0].network_attached:
        raise ResolutionError(
            "CPE_NOT_ATTACHED",
            "This service's CPE is not attached to the network graph",
        )

    path = [
        _node_from_item(db, item, position, purpose)
        for position, item in enumerate(path_items)
    ]
    _attach_ports(db, path, client_service.company_id)
    steps = [n for n in path if not n.is_passive and n.playbook_id is not None]

    errors: List[Dict[str, Any]] = [
        {
            "code": "PLAYBOOK_NOT_BOUND",
            "position": n.position,
            "item_id": str(n.item_id),
            "device_type_id": str(n.device_type_id),
            "device_type_name": n.device_type_name,
            "category": (n.category_key or "").lower(),
            "detail": (
                f"'{n.device_type_name}' has no {purpose} playbook bound, and "
                f"its category is not marked passive"
            ),
        }
        for n in path
        if not n.is_passive and n.playbook_id is None
    ]

    # Every playbook on the path must be active and owned by this company. An
    # inactive playbook is a deliberate operator action ("stop running this")
    # and must not be silently skipped.
    playbooks = {
        pb.id: pb
        for pb in db.execute(
            sa.select(Playbook).where(
                Playbook.id.in_([n.playbook_id for n in steps] or [None])
            )
        ).scalars()
    }
    inactive = [
        n for n in steps
        if playbooks.get(n.playbook_id) is None
        or not playbooks[n.playbook_id].is_active
        or playbooks[n.playbook_id].company_id != client_service.company_id
    ]
    if inactive:
        raise ResolutionError(
            "PLAYBOOK_INACTIVE",
            f"The {purpose} playbook bound to "
            f"'{inactive[0].device_type_name}' is not active",
        )
    for n in steps:
        n.playbook_version = playbooks[n.playbook_id].version

    missing_service_params: List[Dict[str, Any]] = []
    shared: Dict[str, Any] = {"service.id": str(client_service.id)}

    # getattr, not attribute access: resolution runs against detached/partial
    # ClientService objects too (the workflow engine, tests), and a missing
    # relationship must degrade to "no client variables", never crash a job.
    client = getattr(client_service, "client", None)
    if client is not None:
        shared["client.id"] = str(client.id)
        shared["client.name"] = client.name or ""
        # cc1: the short per-company client id (legacy "CO0648" or random).
        shared["client.code"] = getattr(client, "code", None) or ""
        shared["client.email"] = client.email or ""
        shared["client.phone"] = client.phone or ""
        shared["client.address"] = client.address or ""
        # The tenant's own client attributes, exactly as a service plan's
        # provisioning parameters work — a per-subscriber value an operator
        # defines in the CRM and templates in a playbook.
        for key, value in iter_client_custom_fields(client):
            token = f"client.{key}"
            # Built-in fields win: a custom field keyed `name` must not shadow
            # the subscriber's actual name in a template that already reads it.
            if token in shared:
                continue
            shared[token] = value

    plan = getattr(client_service, "service_plan", None)
    if plan is not None:
        shared["service_plan.id"] = str(plan.id)
        shared["service_plan.name"] = plan.name or ""
        if plan.download_mbps is not None:
            shared["service_plan.download_mbps"] = plan.download_mbps
        if plan.upload_mbps is not None:
            shared["service_plan.upload_mbps"] = plan.upload_mbps
        # Tenant-authored rows land under the plan's own namespace instead of
        # being flattened into the global one, so a plan parameter can never
        # collide with (or shadow) a system variable.
        #
        # A `service`-scoped row is DECLARED by the plan but VALUED by this
        # service, and still resolves under the plan's namespace: the playbook
        # author writes {{service_plan.<key>}} either way and never has to edit
        # a template when a parameter's scope changes.
        service_values = _service_param_values(client_service)
        for key, value, scope in iter_provisioning_params(plan.provisioning_params):
            if scope == SCOPE_SERVICE:
                value = service_values.get(key)
                if _is_blank(value):
                    # Recorded, not raised: whether this is fatal depends on
                    # the playbook actually referencing it (checked below).
                    missing_service_params.append({
                        "code": "MISSING_SERVICE_PARAM",
                        "key": key,
                        "token": f"service_plan.{key}",
                        "service_plan_name": plan.name,
                        "detail": (
                            f"'{key}' is declared per-service on plan "
                            f"'{plan.name}' but this service has no value for it"
                        ),
                    })
                    continue
            shared[f"service_plan.{key}"] = value

    # cpe.* — the leaf that triggered the run. Always path[0]; ordering is
    # leaf -> root by contract, not by luck.
    shared.update(build_device_frame(path[0], "cpe"))

    # path.<category_key>.* — nearest-to-the-CPE wins. Because `path` is
    # leaf -> root, taking the FIRST occurrence of each category IS "nearest",
    # with no comparison and no tie-break needed. A tree gives a total order
    # along a path, so this is unambiguous by construction.
    #
    # Passives are addressable too: a playbook may legitimately want the serial
    # of the splitter a subscriber hangs off for a description field, even
    # though nothing is ever configured ON it.
    path_nodes: Dict[str, ResolvedNode] = {}
    for node in path:
        key = (node.category_key or "").lower()
        if not key or key in path_nodes or not _REFERENCEABLE_KEY.match(key):
            continue
        path_nodes[key] = node
        shared.update(build_device_frame(node, f"path.{key}"))

    # path.<role>.* — a per-company device-type role ("mufa_principal") for the
    # roles a category cannot tell apart (doc 40 §3.3.1). Emitted only when
    # exactly one node holds it: "nearest wins" would silently pick the wrong
    # splitter of two, so a repeated role is reported instead (ROLE_AMBIGUOUS,
    # when a playbook reads it). A role never overwrites a category frame, and
    # a role named like a category on the path fails closed whatever its
    # holder count — else the category frame silently answers path.<role>.*.
    holders: Dict[str, List[ResolvedNode]] = defaultdict(list)
    for node in path:
        role = (node.path_role or "").strip().lower()
        if role and _REFERENCEABLE_KEY.match(role):
            holders[role].append(node)
    ambiguous_roles: Dict[str, List[str]] = {}
    for role, nodes in holders.items():
        if role in path_nodes:
            raise ResolutionError(
                "ROLE_SHADOWS_CATEGORY",
                f"The path role '{role}' has the same name as a device category "
                f"on this path; rename the role",
                errors=[{
                    "code": "ROLE_SHADOWS_CATEGORY", "role": role,
                    "item_id": str(nodes[0].item_id),
                    "detail": (f"The path role '{role}' has the same name as a "
                               f"device category on this path; rename the role"),
                }],
            )
        if len(nodes) > 1:
            ambiguous_roles[role] = [str(n.item_id) for n in nodes]
            continue
        path_nodes[role] = nodes[0]
        shared.update(build_device_frame(nodes[0], f"path.{role}"))

    device_variables = {n.item_id: build_device_frame(n, "device") for n in steps}

    # PLAYBOOK_NOT_BOUND is fatal for ACTIVATION unconditionally, and for other
    # purposes only when some playbook on the path actually reads a device
    # variable — the same amendment-4 rule that used to govern unresolved chain
    # positions. Refusing to suspend a service because an unrelated OLT lacks a
    # suspend playbook would be worse than suspending what we can.
    if errors:
        fatal = purpose == PURPOSE_ACTIVATION or any(
            _playbook_references_device_variables(playbooks[n.playbook_id])
            for n in steps
            if playbooks.get(n.playbook_id) is not None
        )
        if fatal:
            raise ResolutionError(
                "RESOLUTION_FAILED",
                "One or more devices on this service's path have no playbook",
                errors=errors,
            )

    # A per-service parameter with no value is fatal only when a playbook on
    # this path actually reads it. A SUSPENSION playbook that never templates
    # {{service_plan.pppoe_user}} must not be blocked because some unrelated
    # parameter was left blank.
    referenced_missing = [
        err for err in missing_service_params
        if any(
            _playbook_references_token(playbooks[n.playbook_id], err["token"])
            for n in steps
            if playbooks.get(n.playbook_id) is not None
        )
    ]
    if referenced_missing:
        raise ResolutionError(
            "RESOLUTION_FAILED",
            "One or more per-service provisioning parameters have no value",
            errors=referenced_missing,
        )

    refusals = _refusals(db, client_service, purpose, steps, playbooks, shared,
                         device_variables, path_nodes, ambiguous_roles)
    if refusals:
        raise ResolutionError(
            "RESOLUTION_FAILED",
            "One or more values the playbooks on this path need are missing or "
            "have changed",
            errors=refusals,
        )

    return ResolvedProvisioning(
        path=path,
        steps=steps,
        shared_variables=shared,
        device_variables=device_variables,
        ambiguous_roles=ambiguous_roles,
    )
