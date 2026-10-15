"""
core_connectivity_check job construction (doc 25 §5.1, spec N10/N11): the
playbook definition, its per-company get-or-create, and the one enqueue path
shared by the manual "test connection" button (backend-erp
routers/core_devices.py) and the worker's hourly sweep.

Moved from backend-erp provisioning/worker.py (provisioning worker split,
R1) so the API enqueues a probe without importing the worker. A gate refusal
is the existing `ProvisioningGateError`, never an HTTP exception: the API
maps it to its 409, the sweep skips the item.
"""
from __future__ import annotations

from loguru import logger

from database_utils.models.isp import (
    InventoryItem,
    Playbook,
    ProvisioningJob,
    ProvisioningTrigger,
)
from database_utils.utils.provisioning_gates import ProvisioningGateError, gate_failure
from database_utils.utils.transport import company_dials_gateway

DEFAULT_PROBE_COMMAND = "display version"


def _connectivity_definition(protocol: str, nat_mode: bool = False) -> dict:
    """doc 25 §5.1 / spec N10 — moved here (Task 9) from routers/core_devices.py
    so the manual "test connection" button and the hourly sweep build the
    exact same playbook definition and can never diverge on ping-vs-NAT
    step selection. See the original docstring in git history for the full
    rationale; unchanged behavior, only the module moved."""
    steps = []
    if not nat_mode:
        steps.append({
            "name": "ping core",
            "driver": "ping",
            "target_item_id": "{{input.item_id}}",
            "template": "{{input.mgmt_host}}",
        })
    steps.append({
        "name": "cli probe",
        "driver": protocol,
        "target_item_id": "{{input.item_id}}",
        "template": "{{input.probe_command}}",
    })
    return {
        "variables": [
            {"key": "item_id", "required": True},
            {"key": "mgmt_host", "required": True},
            {"key": "probe_command", "default": DEFAULT_PROBE_COMMAND},
        ],
        "steps": steps,
    }


def _get_or_create_connectivity_playbook(db, company_id, protocol: str, nat_mode: bool = False) -> Playbook:
    """Get-or-create per company at first use (doc 25 §5.1), keyed on
    (protocol, nat_mode) so a cached playbook is never reused across dial
    targets (spec N10).

    `nat_mode` (and the stored `-nat` suffix) survived tr1_transport_axis
    deliberately. The NAME IS THE GET-OR-CREATE KEY: renaming it to match the new
    `dial_target` vocabulary would orphan every existing
    `core_connectivity_check_*-nat` playbook row and silently create a duplicate
    per tenant. The word still describes the physical situation — a dst-nat port
    mapping — what died is the `mode` enum, not the NAT."""
    name = f"core_connectivity_check_{protocol}"
    if nat_mode:
        name += "-nat"
    playbook = (
        db.query(Playbook)
        .filter(Playbook.company_id == company_id, Playbook.name == name)
        .first()
    )
    desired_definition = _connectivity_definition(protocol, nat_mode=nat_mode)
    if playbook is not None:
        # Self-heal, updated IN PLACE (same row/id, never a new key): the name
        # already fully encodes (protocol, nat_mode), so a mismatch here can
        # only mean this row's `definition` was written by an earlier version
        # of `_connectivity_definition` (e.g. spec N10's evidence: a
        # gateway-dial row that predates the "-nat" ping-step-drop ships a
        # stale ICMP `ping` step against the device's own address forever,
        # since get-or-create by itself never revisits an existing row). A
        # SYSTEM playbook has nothing operator-authored to preserve, so
        # updating in place is safe and is the only fix that isn't a silent
        # no-op against already-created rows in dev/prod.
        if playbook.definition != desired_definition:
            logger.bind(company_id=str(company_id), playbook=name).warning(
                "core_connectivity_check playbook definition drifted; refreshing in place"
            )
            playbook.definition = desired_definition
            db.flush()
        return playbook

    description = f"System playbook: core device connectivity check ({protocol} login probe)"
    if not nat_mode:
        description = f"System playbook: core device connectivity check (ping + {protocol} login probe)"

    playbook = Playbook(
        company_id=company_id,
        name=name,
        description=description,
        definition=desired_definition,
        is_active=True,
    )
    db.add(playbook)
    db.flush()
    return playbook


def enqueue_connectivity_check(
    db,
    item: InventoryItem,
    *,
    idempotency_key: str,
    triggered_by: ProvisioningTrigger = ProvisioningTrigger.API,
    triggered_by_user_id=None,
    scheduled_for=None,
) -> ProvisioningJob:
    """The one job-construction path for a core_connectivity_check job (doc
    25 §5.1, spec N10 nat_mode selection) — shared by the manual "test
    connection" button (`routers/core_devices.py`) and the hourly sweep
    (`enqueue_connectivity_sweep` below). Resolves protocol/nat_mode from the
    item and its company's provisioning_settings row exactly once, so
    the two callers can never disagree on which playbook a device gets.

    Always enforces the enable gates (kill switch / tenant disabled /
    device-type disabled) — raises ProvisioningGateError([failure]) if
    blocked; `errors[0]` is exactly the 409 body backend-erp's
    `enforce_provisioning_gates` returns, and the router maps it back to that
    409. The sweep catches the error and skips the item quietly; it is not itself a
    reason to relax enqueue-time enforcement, because a gate-blocked item
    that skips the gate check here would otherwise get a QUEUED job created
    (and a `provisioning_job` UPDATED event fired) every sweep tick forever
    for every disabled tenant, since a claim-time-blocked job never stamps
    `mgmt_last_check_at` and so never stops re-qualifying as stale.

    `scheduled_for` is written in the SAME commit as the row (R21): the sweep
    used to set its stagger after the commit, so a worker could claim the job
    in between and the late write landed on a running row."""
    protocol = item.cli_protocol or "ssh"
    nat_mode = company_dials_gateway(db, item.company_id)

    playbook = _get_or_create_connectivity_playbook(db, item.company_id, protocol, nat_mode=nat_mode)

    failure = gate_failure(db, item.company_id, playbook, item.device_type, dry_run=False)
    if failure:
        raise ProvisioningGateError([failure])

    probe_command = DEFAULT_PROBE_COMMAND
    if item.device_type and isinstance(item.device_type.default_attributes, dict):
        probe_command = item.device_type.default_attributes.get("probe_command") or DEFAULT_PROBE_COMMAND

    job = ProvisioningJob(
        company_id=item.company_id,
        playbook_id=playbook.id,
        inventory_item_id=item.id,  # device lock applies (canon C11)
        variables={
            "input.item_id": str(item.id),
            "input.mgmt_host": item.mgmt_host,
            "input.probe_command": probe_command,
        },
        idempotency_key=idempotency_key,
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
        scheduled_for=scheduled_for,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
