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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database_utils.models.isp import (
    ClientService,
    DeviceTypePlaybook,
    InventoryItem,
    InventoryItemPlaybook,
    Playbook,
    PURPOSE_ACTIVATION,
)
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
    }


def _playbook_references_token(playbook: Playbook, token: str) -> bool:
    """Whether the playbook's own definition templates this exact token.

    Used to decide whether a missing per-service parameter is fatal: declaring
    `pppoe_user` per-service must not block a SUSPENSION playbook that never
    reads it. Same posture as `_playbook_references_device_variables` — fail
    safe (treat as referenced) when the definition cannot be introspected."""
    try:
        blob = json.dumps(playbook.definition)
    except (TypeError, ValueError):
        return True
    return re.search(
        r"\{\{\s*" + re.escape(token) + _FILTER_SUFFIX + r"\s*\}\}", blob
    ) is not None


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
    return bool(_DEVICE_VARIABLE_PATTERN.search(blob))


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
    )
    if not node.is_passive:
        node.playbook_id, node.playbook_source = resolve_playbook_for(db, item, purpose)
    return node


def resolve_provisioning(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
) -> ResolvedProvisioning:
    """Resolve a service's configuration path, its per-node playbooks and its
    variable frames — or raise ResolutionError.

    Algorithm (doc 35 §3.2):

    1. client_service.cpe_item_id unset            -> CPE_NOT_SET
    2. that CPE not attached to the graph          -> CPE_NOT_ATTACHED
    3. path = resolve_path(cpe), ordered LEAF -> ROOT
    4. a node whose category is_passive contributes nothing but stays on `path`
    5. every other node resolves node override -> device-type default -> none
    6. an ACTIVE node with no playbook for `purpose` is reported
       PLAYBOOK_NOT_BOUND — fatal for ACTIVATION, non-fatal otherwise

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

    missing_service_params: List[Dict[str, Any]] = []
    shared: Dict[str, Any] = {"service.id": str(client_service.id)}

    # getattr, not attribute access: resolution runs against detached/partial
    # ClientService objects too (the workflow engine, tests), and a missing
    # relationship must degrade to "no client variables", never crash a job.
    client = getattr(client_service, "client", None)
    if client is not None:
        shared["client.id"] = str(client.id)
        shared["client.name"] = client.name or ""
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
    seen: set = set()
    for node in path:
        key = (node.category_key or "").lower()
        if not key or key in seen or not _REFERENCEABLE_KEY.match(key):
            continue
        seen.add(key)
        shared.update(build_device_frame(node, f"path.{key}"))

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

    return ResolvedProvisioning(
        path=path,
        steps=steps,
        shared_variables=shared,
        device_variables=device_variables,
    )
