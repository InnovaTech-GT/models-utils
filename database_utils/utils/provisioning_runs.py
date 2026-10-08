"""Service-path provisioning runs (doc 35 §5).

Lives in models-utils, not backend-erp, for the same reason
provisioning_resolution does: the workflow engine's ENQUEUE_PROVISIONING path
opens runs too, and the engine cannot import backend-erp. Import direction is
strictly downward.

A run is the container; each configured device gets its own child job. Children
are created LAZILY, one at a time, in `plan` order, so at most one child of a
run is QUEUED or RUNNING at any moment. Two consequences worth stating plainly:

- No new ProvisioningJobStatus value was needed. A "BLOCKED" state would have
  had to be understood by every status consumer across three services and the
  automations run list.
- Producers never lock. Every child is inserted with device_lock_key NULL;
  only the worker's claim writes the lock (provisioning-concurrency fix). A
  lock written at INSERT made the commit that recorded child N's outcome fail
  whenever child N+1's device was busy with another run or a probe (the
  2026-10-06 incident). The worker's claim skips a child whose device is held.
- Every run-row write goes through advance_run, which locks the run row and
  no-ops on a terminal run or a stale/duplicate advance, so a late settle can
  never resurrect a finished run or queue a child twice.

Engine v2 (doc 42 §5-§8): the plan is phase-major — every device's
PRECONDITIONS, then CONFIGURATION, then VERIFICATION, in build order (core
bottom-up, CPE last; teardown purposes reversed) — one entry and one child per
(device, phase). A precondition failure ends the run with nothing changed; a
configuration or verification failure APPENDS one ROLLBACK entry per device
that ran a step, in reverse configuration order, each running that playbook's
own rollback (append_rollback). Every child executes the definition snapshot
in frames["definitions"], never the live row. close_run is the only writer of
a terminal run status and fires RUN_CLOSED_LISTENERS (doc 42 §6.5). Office
actions: revert_run ("Revertir") and retry_rollback ("Reintentar reversión").
A retry after a failure is a NEW run on the same key (create_or_get_run).

WHAT THIS MODULE DOES NOT DO: it does not evaluate the provisioning gates
(kill switch, dry-run gate). Those live in backend-erp and are called by its
routers before create_run, exactly as they are today. The workflow-engine path
still does not call them — a pre-existing gap recorded in doc 33 and doc 35
§10. Closing it here would silently change automation behaviour mid-cycle;
it is filed, not smuggled in.
"""

from __future__ import annotations

import json
import secrets as _secrets
import uuid
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database_utils.models.isp import (
    PHASE_CONFIGURATION,
    PHASE_PRECONDITIONS,
    PHASE_ROLLBACK,
    PHASE_VERIFICATION,
    PURPOSE_ACTIVATION,
    PURPOSE_DEPROVISION,
    TEARDOWN_PURPOSES,
    ClientService,
    DeviceActionLog,
    Playbook,
    ProvisioningJob,
    ProvisioningJobStatus,
    ProvisioningRun,
    ProvisioningTrigger,
)
from database_utils.schemas.playbook import (
    CLI_DRIVERS,
    NO_UNDO_DRIVERS,
    SECRET_ALPHABET,
    SESSION_PROBE_STEP,
    job_steps,
    normalize_definition,
)
from database_utils.utils import crypto
from database_utils.utils.acs_bootstrap import acs_values, reads_acs
from database_utils.utils.provisioning_resolution import (
    ResolutionError,
    ResolvedNode,
    ResolvedProvisioning,
    resolve_provisioning,
)
from database_utils.utils.timezone_utils import make_aware_gt, now_gt

# Statuses in which a run (or job) is still live. Must mirror the predicate on
# uq_provisioning_run_company_idem — if these disagree, the dedupe check and
# the unique index disagree, and one of them starts raising IntegrityError.
IN_FLIGHT = (
    ProvisioningJobStatus.QUEUED,
    ProvisioningJobStatus.RUNNING,
    ProvisioningJobStatus.PENDING_INFORM,
)

TERMINAL_OK = (ProvisioningJobStatus.SUCCEEDED,)


# A run with no in-flight child is only "stranded" once it has been quiet this
# long; younger ones may still have a settle in progress.
STRANDED_RUN_GRACE = timedelta(seconds=30)
# ...and one quiet for longer than this is not simply advanced: the reaper
# repairs a fresh stranding within a minute, so anything older would configure
# the next device from a plan resolved long ago. Engine v2 (doc 42 §8.4): a run
# stranded in PRECONDITIONS (or a legacy / dry run) is closed FAILED, one in
# CONFIGURATION / VERIFICATION enters ROLLBACK (the safe direction), and one in
# ROLLBACK keeps advancing.
STRANDED_RUN_MAX_AGE = timedelta(hours=1)

# Run outcome codes (doc 42 §6.3), run.error_code.
PRECONDITION_FAILED = "PRECONDITION_FAILED"
CONFIGURATION_FAILED = "CONFIGURATION_FAILED"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
CANCELLED = "CANCELLED"
STRANDED_RUN_EXPIRED = "STRANDED_RUN_EXPIRED"
ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE"
REVERTED = "REVERTED"
NO_ROLLBACK_DEFINED = "NO_ROLLBACK_DEFINED"

# Per-step attempt budget by phase (doc 42 §8.2.3). A child's max_attempts
# (the claim-time backstop) is this times its step count.
PHASE_MAX_ATTEMPTS = {
    PHASE_PRECONDITIONS: 3,
    PHASE_CONFIGURATION: 3,
    PHASE_VERIFICATION: 3,
    PHASE_ROLLBACK: 5,
}
_FORWARD_PHASES = (PHASE_PRECONDITIONS, PHASE_CONFIGURATION, PHASE_VERIFICATION)

# Run-closed hooks (doc 42 §6.5). close_run calls each with (db, run) for a
# non-dry run, inside its own SAVEPOINT; a raising listener is logged and
# rolled back alone, the run outcome is kept. backend-erp's
# provisioning/run_events.py registers the one listener at import.
# ponytail: an in-process list, not an outbox table; rows are produced eagerly.
RUN_CLOSED_LISTENERS: List[Callable[[Session, ProvisioningRun], None]] = []


class RunNotRevertible(Exception):
    """409 RUN_NOT_REVERTIBLE from revert_run / retry_rollback (doc 42 §7.6)."""

    code = "RUN_NOT_REVERTIBLE"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def run_idempotency_key(client_service_id, purpose: str, dry_run: bool = False) -> str:
    """Stable key for "this service, this purpose, this mode" — the ONE key
    every producer uses (manual /provision, the lifecycle hooks, the workflow
    engine's default), so they dedupe against each other instead of racing.

    DEPROVISION is the bare `deprovision-{id}`: that is literally what the
    retired 'service-removal' workflow template composes, so a still-active
    copy dedupes against the native lifecycle run instead of opening a second
    one. Every other purpose keeps a purpose suffix so a queued ACTIVATION
    cannot block a SUSPENSION. A dry run gets `-dry` so it never collides with
    (or blocks) the live run it precedes.
    """
    if str(purpose).upper() == PURPOSE_DEPROVISION:
        key = f"deprovision-{client_service_id}"
    else:
        key = f"path-provision-{client_service_id}-{purpose.lower()}"
    return key + "-dry" if dry_run else key


def find_in_flight_run(
    db: Session, company_id, idempotency_key: str
) -> Optional[ProvisioningRun]:
    if not idempotency_key:
        return None
    return db.execute(
        sa.select(ProvisioningRun).where(
            ProvisioningRun.company_id == company_id,
            ProvisioningRun.idempotency_key == idempotency_key,
            ProvisioningRun.status.in_(IN_FLIGHT),
        )
    ).scalars().first()


def _child_variables(run: ProvisioningRun, item_id: str) -> Dict[str, Any]:
    """shared | device — one flat dict, exactly what the renderer expects.

    The renderer contract (doc 33) is that `variables` is a flat dict whose keys
    are the whole dotted strings. Splitting the frames on the run and merging
    them here keeps that contract intact while letting `device.*` differ per
    node.
    """
    frames = run.frames or {}
    merged = dict(frames.get("shared") or {})
    merged.update((frames.get("device") or {}).get(str(item_id)) or {})
    return merged


# --------------------------------------------------------------------- plan

def order_for_purpose(steps: List[ResolvedNode], purpose: str) -> List[ResolvedNode]:
    """Configuration order (doc 42 §5). `steps` is resolved.steps, leaf -> root
    (position 0 = the CPE when it is configured). Build order = core
    bottom-up, CPE LAST (founder 9.A); SUSPENSION / DEPROVISION run it
    reversed, so the ONU is de-authorized last (after that it is unreachable)."""
    cpe = [n for n in steps if n.position == 0]
    core = [n for n in steps if n.position != 0]
    build = core + cpe
    return build[::-1] if purpose in TEARDOWN_PURPOSES else build


def _device_label(node: ResolvedNode) -> str:
    ident = node.label or node.serial_number
    return " · ".join(p for p in (node.device_type_name, ident) if p)


def _needs_probe(definition: Dict[str, Any], cpe_in_build_order: bool) -> bool:
    """The implicit preflight session probe (doc 42 §6.4)."""
    if cpe_in_build_order:
        return False
    if not any(s.get("driver") in CLI_DRIVERS
               for key in ("configuration", "verification")
               for s in definition.get(key) or []):
        return False
    pre = definition.get("preconditions") or []
    return not (pre and pre[0].get("driver") in CLI_DRIVERS)


def _step_labels(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"name": s.get("name"), "label": s.get("label") or s.get("name")} for s in steps]


def _plan_entry(node: ResolvedNode, phase: str, steps: List[Dict[str, Any]],
                probe: bool = False) -> Dict[str, Any]:
    entry = {
        "item_id": str(node.item_id),
        "playbook_id": str(node.playbook_id),
        # playbook_version is the dry-run stamping key (doc 42 §9.2).
        "playbook_version": node.playbook_version,
        "category_key": (node.category_key or "").lower(),
        "device_label": _device_label(node),
        "phase": phase,
        "steps": _step_labels(steps),
    }
    if probe:
        entry["probe"] = True
    return entry


def definition_for(run: ProvisioningRun, playbook_id: Any) -> Dict[str, Any]:
    """The normalized definition snapshot a run child executes (doc 42 §6.1);
    never the live playbook row."""
    return ((run.frames or {}).get("definitions") or {}).get(str(playbook_id)) or {}


def _collect_specs(ordered: List[ResolvedNode], definitions: Dict[str, Dict[str, Any]]):
    """Secrets shared across the plan, and the output-key collision check
    (doc 42 §6.1, §9.4)."""
    secret_specs: Dict[str, Dict[str, Any]] = {}
    output_owner: Dict[str, str] = {}
    for node in ordered:
        d = definitions[str(node.playbook_id)]
        for spec in d.get("secrets") or []:
            spec = {"key": spec.get("key"), "length": spec.get("length", 12)}
            if spec["key"] in secret_specs and secret_specs[spec["key"]] != spec:
                raise ResolutionError(
                    "SECRET_SPEC_CONFLICT",
                    f"Two playbooks on this path declare the secret '{spec['key']}' differently")
            secret_specs[spec["key"]] = spec
        for out in d.get("outputs") or []:
            owner = output_owner.setdefault(out.get("key"), str(node.item_id))
            if owner != str(node.item_id):
                raise ResolutionError(
                    "OUTPUT_KEY_CONFLICT",
                    f"Two devices on this path publish the output '{out.get('key')}'")
    return secret_specs


def generate_secret(length: int) -> str:
    return "".join(_secrets.choice(SECRET_ALPHABET) for _ in range(length))


def _encrypt_secrets(run_id, company_id, specs: Dict[str, Dict[str, Any]]):
    values = {key: generate_secret(spec["length"]) for key, spec in specs.items()}
    try:
        return crypto.encrypt_secret(json.dumps(values), company_id, run_id)
    except crypto.CredentialCryptoError as exc:
        raise ResolutionError(
            "SECRETS_KEY_UNAVAILABLE",
            "This path generates secrets but no encryption key is configured") from exc


def decrypt_run_secrets(run: ProvisioningRun) -> Dict[str, str]:
    """{key: value} of the run's generated secrets ({} when none). Callers must
    never log or persist the result (doc 42 §10.2)."""
    if not run.secrets_ciphertext:
        return {}
    return json.loads(crypto.decrypt_secret(
        run.secrets_ciphertext, run.secrets_dek_wrapped, run.secrets_kek_id,
        run.company_id, run.id))


def _queue_child(db: Session, run: ProvisioningRun, position: int) -> Optional[ProvisioningJob]:
    """Create and queue the child at `position`, or None if the plan is done."""
    plan = run.plan or []
    if position >= len(plan):
        return None
    entry = plan[position]
    item_id = entry["item_id"]
    phase = entry.get("phase")
    max_attempts = 3
    if phase is not None:
        steps = job_steps(definition_for(run, entry["playbook_id"]), phase,
                          probe=bool(entry.get("probe")))
        max_attempts = PHASE_MAX_ATTEMPTS[phase] * max(1, len(steps))
        run.phase = phase
    job = ProvisioningJob(
        id=uuid.uuid4(),
        company_id=run.company_id,
        playbook_id=uuid.UUID(entry["playbook_id"]),
        client_service_id=run.client_service_id,
        inventory_item_id=uuid.UUID(item_id),
        variables=_child_variables(run, item_id),
        dry_run=run.dry_run,
        # Derived from the run's key so a child is still individually unique
        # under uq_provisioning_job_company_idem.
        idempotency_key=(
            f"{run.idempotency_key}#{position}" if run.idempotency_key else None
        ),
        triggered_by=run.triggered_by,
        triggered_by_user_id=run.triggered_by_user_id,
        # Never locked at INSERT: the worker's claim takes the device lock.
        device_lock_key=None,
        run_id=run.id,
        run_position=position,
        phase=phase,
        max_attempts=max_attempts,
        status=ProvisioningJobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    return job


def _plan_checks(db: Session, client_service: ClientService, resolved: ResolvedProvisioning,
                 purpose: str, *, ensure: bool):
    """(ordered, definitions, secret_specs) plus every plan-wide refusal that
    needs the snapshotted definitions: SECRET_SPEC_CONFLICT,
    OUTPUT_KEY_CONFLICT (§9.4) and, when a definition reads {{acs.*}}, the
    ACS checks of §9.7 (ensure=True also ensures the CPE's registration)."""
    ordered = order_for_purpose(list(resolved.steps), purpose)
    definitions: Dict[str, Dict[str, Any]] = {}
    for node in ordered:
        pid = str(node.playbook_id)
        if pid not in definitions:
            pb = db.get(Playbook, node.playbook_id)
            definitions[pid] = normalize_definition(dict((pb.definition if pb else None) or {}))
    secret_specs = _collect_specs(ordered, definitions)
    if reads_acs(definitions.values()):
        cpe = next((n for n in resolved.path if n.position == 0), None)
        # The values are checked and discarded: the worker reloads them.
        acs_values(db, client_service.company_id, cpe.serial_number if cpe else None,
                   item_id=cpe.item_id if cpe else None, ensure=ensure)
    return ordered, definitions, secret_specs


def preflight_run(db: Session, client_service: ClientService, resolved: ResolvedProvisioning,
                  purpose: str, dry_run: bool = False) -> None:
    """create_run's plan-wide refusals with NO write (doc 42 §9.4, §9.7), for
    a caller that must refuse before it mutates anything (the lifecycle
    endpoints write the service status before they open the run). Raises the
    same ResolutionError codes create_run would, SECRETS_KEY_UNAVAILABLE
    included."""
    _o, _d, secret_specs = _plan_checks(db, client_service, resolved, purpose, ensure=False)
    if secret_specs and not dry_run:
        _encrypt_secrets(uuid.uuid4(), client_service.company_id, {})


def create_run(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
    dry_run: bool = False,
    triggered_by: ProvisioningTrigger = ProvisioningTrigger.USER,
    triggered_by_user_id=None,
    idempotency_key: Optional[str] = None,
    extra_variables: Optional[Dict[str, Any]] = None,
    resolution: Optional[ResolvedProvisioning] = None,
) -> ProvisioningRun:
    """Resolve the path and open a run with only its first child queued.

    `resolution` may be passed by a caller that already resolved (the manual
    endpoint resolves first so it can 422 with the error list before touching
    anything); otherwise it is resolved here. Either way it is resolved EXACTLY
    ONCE per run — the frames and the definitions are snapshotted so later
    children cannot silently follow a path or a playbook edit the operator
    never saw.

    Engine v2 (doc 42 §6.1): `plan` is phase-major — every device's
    PRECONDITIONS, then CONFIGURATION, then VERIFICATION, each in
    order_for_purpose — one entry per (device, phase) that has steps, plus the
    implicit session probe. A dry run also plans every device's ROLLBACK, in
    reverse configuration order, and never branches. Secrets declared by the
    plan are generated and encrypted here (non-dry only), before anything is
    inserted.

    Raises ResolutionError even when `resolution` is passed: the plan-wide
    checks need the snapshotted definitions, so SECRET_SPEC_CONFLICT,
    OUTPUT_KEY_CONFLICT and SECRETS_KEY_UNAVAILABLE come from here, not from
    resolve_provisioning. Callers map it to 422 like a resolution failure.

    A definition that reads {{acs.*}} (doc 42 §9.7) also raises
    ACS_NOT_CONFIGURED / ACS_SERIAL_CLAIMED / ACS_VALUE_UNSAFE, and a non-dry
    run ensures the CPE's acs_device_registration before any child exists; a
    dry run writes no registration. CR credentials are never minted here
    (founder round 4: the worker generates them per child, in memory).
    """
    resolved = resolution or resolve_provisioning(db, client_service, purpose)

    shared = dict(resolved.shared_variables)
    if extra_variables:
        shared.update(extra_variables)

    ordered, definitions, secret_specs = _plan_checks(
        db, client_service, resolved, purpose, ensure=not dry_run)

    build = purpose not in TEARDOWN_PURPOSES
    plan: List[Dict[str, Any]] = []
    for phase in _FORWARD_PHASES:
        for node in ordered:
            d = definitions[str(node.playbook_id)]
            probe = phase == PHASE_PRECONDITIONS and _needs_probe(d, build and node.position == 0)
            steps = job_steps(d, phase, probe=probe)
            if steps:
                plan.append(_plan_entry(node, phase, steps, probe))
    if dry_run:
        for node in reversed(ordered):
            steps = job_steps(definitions[str(node.playbook_id)], PHASE_ROLLBACK)
            if steps:
                plan.append(_plan_entry(node, PHASE_ROLLBACK, steps))

    run_id = uuid.uuid4()
    sealed = (None, None, None)
    if secret_specs and not dry_run:
        sealed = _encrypt_secrets(run_id, client_service.company_id, secret_specs)

    run = ProvisioningRun(
        id=run_id,
        company_id=client_service.company_id,
        client_service_id=client_service.id,
        purpose=purpose,
        dry_run=dry_run,
        status=ProvisioningJobStatus.QUEUED,
        phase=plan[0]["phase"] if plan else None,
        path=[asdict(n) | {"item_id": str(n.item_id),
                           "device_type_id": str(n.device_type_id),
                           "playbook_id": str(n.playbook_id) if n.playbook_id else None}
              for n in resolved.path],
        plan=plan,
        frames={
            "shared": shared,
            "device": {str(k): v for k, v in resolved.device_variables.items()},
            "definitions": definitions,
        },
        secrets_ciphertext=sealed[0],
        secrets_dek_wrapped=sealed[1],
        secrets_kek_id=sealed[2],
        idempotency_key=idempotency_key
        or run_idempotency_key(client_service.id, purpose, dry_run),
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
    )
    db.add(run)
    db.flush()

    _queue_child(db, run, 0)
    return run


# --------------------------------------------------------------------- closing

def close_run(db: Session, run: ProvisioningRun, status: ProvisioningJobStatus,
              error_code: Optional[str] = None, error: Optional[str] = None) -> None:
    """The ONLY writer of a terminal run status (doc 42 §6.5). Sets status,
    finished_at and (when given) error_code / error, then — for a non-dry run —
    calls every RUN_CLOSED_LISTENERS entry with (db, run), each in its own
    SAVEPOINT. A listener that raises is logged and rolled back alone."""
    run.status = status
    if error_code is not None:
        run.error_code = error_code
    if error is not None:
        run.error = error
    run.finished_at = now_gt()
    db.flush()
    if run.dry_run:
        return
    for listener in list(RUN_CLOSED_LISTENERS):
        try:
            with db.begin_nested():
                listener(db, run)
        except Exception:  # noqa: BLE001 — the run outcome must survive a listener
            logger.bind(run_id=str(run.id)).exception("close_run: run-closed listener failed")


def _merge_outputs(run: ProvisioningRun, job: ProvisioningJob, entry: Dict[str, Any]) -> None:
    """job.log.outputs -> run.outputs, last writer per (item_id, key); on every
    terminal child, success or not (a broken reading still shows, ok=false)."""
    outs = (job.log or {}).get("outputs") or []
    if not outs:
        return
    merged = list(run.outputs or [])
    for out in outs:
        row = dict(out) | {"item_id": entry["item_id"], "position": job.run_position,
                           "category_key": entry.get("category_key")}
        merged = [o for o in merged
                  if (o.get("item_id"), o.get("key")) != (row["item_id"], row.get("key"))]
        merged.append(row)
    run.outputs = merged


def _last_entries(job: ProvisioningJob) -> Dict[str, Dict[str, Any]]:
    """Last step-log entry per step name (a retried or resumed step appends)."""
    last: Dict[str, Dict[str, Any]] = {}
    for e in (job.log or {}).get("steps") or []:
        if isinstance(e, dict) and e.get("name") is not None:
            last.pop(e["name"], None)
            last[e["name"]] = e
    return last


def ran_steps(job: ProvisioningJob) -> List[str]:
    """Configuration steps of `job` that may have changed the device (doc 42
    §7.3, §8.2.3): ANY entry of the step SUCCEEDED, or FAILED at stage
    `command` (or an unknown stage — conservative), or is anything other than
    SKIPPED — so a step that failed at `command` and was retried counts as ran
    whatever its final outcome; plus the step interrupted by a crash
    (`log.interrupted_step`, the step NAME backend-erp's _finish copies from
    the journal before stripping it, doc 42 §8.3). An entry SKIPPED or FAILED
    at stage `connect` / `render` sent nothing. The session probe never
    counts."""
    ran: List[str] = []
    for e in (job.log or {}).get("steps") or []:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if name is None or name == SESSION_PROBE_STEP or name in ran or e.get("status") == "SKIPPED":
            continue
        stage = (e.get("detail") or {}).get("stage")
        if e.get("status") == "FAILED" and stage in ("connect", "render"):
            continue
        ran.append(name)
    name = (job.log or {}).get("interrupted_step")
    if isinstance(name, str) and name != SESSION_PROBE_STEP and name not in ran:
        ran.append(name)
    return ran


def _label_of(entry: Dict[str, Any], name: Optional[str]) -> Optional[str]:
    for s in entry.get("steps") or []:
        if s.get("name") == name:
            return s.get("label") or name
    return name


def _failed_steps(job: ProvisioningJob, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"device": entry.get("device_label"),
         "step": e.get("label") or _label_of(entry, name),
         "code": e.get("code") or (e.get("detail") or {}).get("code"),
         "display": e.get("display") or e.get("error")}
        for name, e in _last_entries(job).items() if e.get("status") == "FAILED"
    ]


def _failure_text(job: ProvisioningJob, entry: Dict[str, Any]) -> str:
    """"<device> · <step label>: <display>" for the step that failed."""
    failed = _failed_steps(job, entry)
    if failed:
        f = failed[-1]
        return f"{f['device']} · {f['step']}: {f['display'] or f['code'] or job.status.value}"
    return f"{entry.get('device_label')}: {job.error or job.status.value}"


# --------------------------------------------------------------------- rollback

def _children(db: Session, run: ProvisioningRun) -> List[ProvisioningJob]:
    return db.execute(
        sa.select(ProvisioningJob).where(ProvisioningJob.run_id == run.id)
        .order_by(ProvisioningJob.run_position)
    ).scalars().all()


def _plan_rollback(db: Session, run: ProvisioningRun, items: Optional[Iterable[str]] = None):
    """(touched, entries, no_rollback) for the run's CONFIGURATION children
    (doc 42 §7.1): touched = items with a non-empty ran_steps, in reverse
    configuration order; entries = the ROLLBACK plan entries to append;
    no_rollback = touched items that changed the device but have no rollback."""
    wanted = None if items is None else {str(i) for i in items}
    plan = run.plan or []
    cfg = [j for j in _children(db, run)
           if (plan[j.run_position].get("phase") if j.run_position < len(plan) else None)
           == PHASE_CONFIGURATION]
    touched, entries, no_rollback = [], [], []
    for job in sorted(cfg, key=lambda j: j.run_position, reverse=True):
        entry = plan[job.run_position]
        if wanted is not None and entry["item_id"] not in wanted:
            continue
        ran = ran_steps(job)
        if not ran:
            continue
        touched.append(entry["item_id"])
        d = definition_for(run, entry["playbook_id"])
        rollback = d.get("rollback") or []
        if not rollback:
            drivers = {s.get("name"): s.get("driver") for s in d.get("configuration") or []}
            if not all(drivers.get(name) in NO_UNDO_DRIVERS for name in ran):
                no_rollback.append(entry["item_id"])
            continue
        steps = []
        for s in _step_labels(rollback):
            undoes = next((r.get("undoes") for r in rollback if r.get("name") == s["name"]), None)
            steps.append(s | {"skip": True} if undoes and undoes not in ran else s)
        if all(s.get("skip") for s in steps):
            continue
        entries.append({k: v for k, v in entry.items() if k not in ("probe",)}
                       | {"phase": PHASE_ROLLBACK, "steps": steps, "ran_steps": ran})
    return touched, entries, no_rollback


def _record_rollback(run: ProvisioningRun, entries: List[Dict[str, Any]],
                     no_rollback: List[str]) -> None:
    frames = dict(run.frames or {})
    state = dict(frames.get("rollback") or {})
    state["no_rollback"] = sorted(set(state.get("no_rollback") or []) | set(no_rollback))
    frames["rollback"] = state
    run.frames = frames
    if entries:
        run.plan = list(run.plan or []) + entries


def append_rollback(db: Session, run: ProvisioningRun,
                    items: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """The one builder of ROLLBACK entries (doc 42 §7.6): computes ran_steps per
    item from the CONFIGURATION children, appends one entry per device needing
    rollback in reverse configuration order (reassigning `plan`, the forward
    plan stays a stable prefix), and records NO_ROLLBACK_DEFINED devices.
    `items` limits it to those item ids (the rollback retry). Returns the
    appended entries; the caller queues the first."""
    _, entries, no_rollback = _plan_rollback(db, run, items)
    _record_rollback(run, entries, no_rollback)
    db.flush()
    return entries


def _set_cause(run: ProvisioningRun, code: str, error: Optional[str]) -> None:
    frames = dict(run.frames or {})
    frames["rollback"] = {"cause_code": code, "cause_error": error, "no_rollback": []}
    run.frames = frames
    run.error_code = code
    run.error = error


def _rollback_incomplete_text(run: ProvisioningRun, details: List[Dict[str, Any]]) -> str:
    cause = ((run.frames or {}).get("rollback") or {}).get("cause_error") or ""
    return f"{cause}\n{json.dumps(details, ensure_ascii=False, default=str)}".strip()


def _close_after_rollback_planned(db: Session, run: ProvisioningRun,
                                  entries: List[Dict[str, Any]],
                                  no_rollback: List[str]) -> Optional[ProvisioningJob]:
    """Queue the first appended entry, or close at once when none was needed."""
    if entries:
        run.phase = PHASE_ROLLBACK
        run.status = ProvisioningJobStatus.RUNNING
        return _queue_child(db, run, len(run.plan) - len(entries))
    if no_rollback:
        details = [{"device": _item_label(run, i), "code": NO_ROLLBACK_DEFINED}
                   for i in no_rollback]
        close_run(db, run, ProvisioningJobStatus.FAILED, ROLLBACK_INCOMPLETE,
                  _rollback_incomplete_text(run, details))
    else:
        close_run(db, run, ProvisioningJobStatus.ROLLED_BACK)
    return None


def _item_label(run: ProvisioningRun, item_id: str) -> Optional[str]:
    return next((e.get("device_label") for e in run.plan or [] if e.get("item_id") == item_id),
                item_id)


def _enter_rollback(db: Session, run: ProvisioningRun, code: str, error: Optional[str],
                    cancelled: bool = False) -> Optional[ProvisioningJob]:
    """A forward failure (doc 42 §6.3): the outcome follows the rollback SET.
    (a) nothing ran anywhere -> FAILED / CANCELLED, devices untouched;
    (b) NO_ROLLBACK_DEFINED and no entry -> FAILED / ROLLBACK_INCOMPLETE;
    (c) otherwise phase ROLLBACK and the first entry is queued."""
    _set_cause(run, code, error)
    touched, entries, no_rollback = _plan_rollback(db, run)
    if not touched:
        close_run(db, run, ProvisioningJobStatus.CANCELLED if cancelled
                  else ProvisioningJobStatus.FAILED)
        return None
    _record_rollback(run, entries, no_rollback)
    return _close_after_rollback_planned(db, run, entries, no_rollback)


def _finish_rollback(db: Session, run: ProvisioningRun) -> None:
    """Last ROLLBACK entry done: ROLLED_BACK when the LATEST rollback child per
    item SUCCEEDED and no device is NO_ROLLBACK_DEFINED, else FAILED /
    ROLLBACK_INCOMPLETE with the failed steps listed (doc 42 §6.3, §7.4)."""
    plan = run.plan or []
    latest: Dict[str, ProvisioningJob] = {}
    for job in _children(db, run):
        if job.run_position < len(plan) and plan[job.run_position].get("phase") == PHASE_ROLLBACK:
            latest[plan[job.run_position]["item_id"]] = job
    no_rollback = ((run.frames or {}).get("rollback") or {}).get("no_rollback") or []
    failed = [j for j in latest.values() if j.status not in TERMINAL_OK]
    if not failed and not no_rollback:
        close_run(db, run, ProvisioningJobStatus.ROLLED_BACK)
        return
    details: List[Dict[str, Any]] = []
    for job in failed:
        entry = plan[job.run_position]
        steps = _failed_steps(job, entry) or [
            {"device": entry.get("device_label"), "step": None,
             "code": job.error or job.status.value, "display": None}]
        details += steps
        db.add(DeviceActionLog(
            id=uuid.uuid4(), company_id=run.company_id, actor_kind="system",
            device_kind=entry.get("category_key"), device_identity=entry.get("device_label"),
            action="rollback_incomplete", provisioning_job_id=job.id,
            detail={"run_id": str(run.id), "item_id": entry["item_id"], "steps": steps},
        ))
    details += [{"device": _item_label(run, i), "code": NO_ROLLBACK_DEFINED} for i in no_rollback]
    close_run(db, run, ProvisioningJobStatus.FAILED, ROLLBACK_INCOMPLETE,
              _rollback_incomplete_text(run, details))


# --------------------------------------------------------------------- advance

def _finish_dry_run(db: Session, run: ProvisioningRun) -> None:
    jobs = _children(db, run)
    ok = bool(jobs) and all(j.status in TERMINAL_OK for j in jobs)
    close_run(db, run, ProvisioningJobStatus.SUCCEEDED if ok else ProvisioningJobStatus.FAILED)
    if not ok:
        return
    # doc 42 §9.2: stamp only a playbook whose live version still equals the
    # version this run planned — a playbook edited mid-run was not simulated.
    for entry in run.plan or []:
        pb = db.get(Playbook, uuid.UUID(entry["playbook_id"]))
        if pb is not None and pb.version == entry.get("playbook_version"):
            pb.last_dry_run_version = pb.version
    db.flush()


def _succeed(db: Session, run: ProvisioningRun) -> None:
    close_run(db, run, ProvisioningJobStatus.SUCCEEDED)
    # The path this service was provisioned against is now the path it sits
    # on, so any re-parent drift recorded earlier is settled.
    if run.purpose == PURPOSE_ACTIVATION:
        svc = db.get(ClientService, run.client_service_id)
        if svc is not None:
            svc.path_changed_at = None
            db.flush()


def _advance_legacy(db: Session, run: ProvisioningRun, job: ProvisioningJob):
    """A plan entry without `phase` (a run opened before engine v2): any
    non-success stops the run and takes that status (the pre-v2 rule)."""
    if job.status not in TERMINAL_OK:
        close_run(db, run, job.status)
        return None
    nxt = _queue_child(db, run, (job.run_position or 0) + 1)
    if nxt is None:
        if run.dry_run:
            close_run(db, run, ProvisioningJobStatus.SUCCEEDED)
        else:
            _succeed(db, run)
    else:
        run.status = ProvisioningJobStatus.RUNNING
        db.flush()
    return nxt


def _lock_run(db: Session, run_id) -> Optional[ProvisioningRun]:
    return db.execute(
        sa.select(ProvisioningRun)
        .where(ProvisioningRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()


def advance_run(db: Session, job: ProvisioningJob) -> Optional[ProvisioningJob]:
    """Called when a job reaches a terminal state. Returns the next child, if any.

    A standalone job (run_id NULL) is a no-op — ACS reboots and connectivity
    probes must keep behaving exactly as they did.

    Locks the run row (lock order everywhere: job row, then run row) and is a
    no-op when:
      - the run is already terminal — a late settle never resurrects it;
      - `job` is itself still in flight;
      - a later child already exists — a duplicate or stale advance (reaped
        executor, cancel racing a settle, repair_stranded_runs) queues nothing.

    Otherwise (doc 42 §6.3): merge the child's outputs into the run, then
      PRECONDITIONS  ok -> next; failed -> FAILED/PRECONDITION_FAILED, cancelled
                     -> CANCELLED; never a rollback (nothing was changed)
      CONFIGURATION / VERIFICATION  ok -> next, the last one SUCCEEDED; else
                     the run enters ROLLBACK (or closes, by the rollback set)
      ROLLBACK       any outcome -> next rollback entry; rollback continues
                     past failures; the last one decides ROLLED_BACK vs
                     ROLLBACK_INCOMPLETE
      dry run        next entry whatever the outcome; SUCCEEDED only if every
                     child succeeded
    """
    if job.run_id is None:
        return None
    run = _lock_run(db, job.run_id)
    if run is None or run.status not in IN_FLIGHT:
        return None
    if job.status in IN_FLIGHT:
        return None
    later = db.execute(
        sa.select(ProvisioningJob.id).where(
            ProvisioningJob.run_id == run.id,
            ProvisioningJob.run_position > (job.run_position or 0),
        ).limit(1)
    ).first()
    if later is not None:
        return None

    position = job.run_position or 0
    plan = run.plan or []
    entry = plan[position] if position < len(plan) else {}
    phase = entry.get("phase")
    if phase is None:
        return _advance_legacy(db, run, job)

    _merge_outputs(run, job, entry)
    ok = job.status in TERMINAL_OK
    cancelled = job.status == ProvisioningJobStatus.CANCELLED

    if run.dry_run or ok or phase == PHASE_ROLLBACK:
        nxt = _queue_child(db, run, position + 1)
        if nxt is not None:
            run.status = ProvisioningJobStatus.RUNNING
            db.flush()
            return nxt
        if run.dry_run:
            _finish_dry_run(db, run)
        elif phase == PHASE_ROLLBACK:
            _finish_rollback(db, run)
        else:
            _succeed(db, run)
        return None

    if phase == PHASE_PRECONDITIONS:
        if cancelled:
            close_run(db, run, ProvisioningJobStatus.CANCELLED, CANCELLED)
        else:
            close_run(db, run, ProvisioningJobStatus.FAILED, PRECONDITION_FAILED,
                      _failure_text(job, entry))
        return None

    code = CANCELLED if cancelled else (
        CONFIGURATION_FAILED if phase == PHASE_CONFIGURATION else VERIFICATION_FAILED)
    return _enter_rollback(db, run, code, _failure_text(job, entry), cancelled=cancelled)


# --------------------------------------------------------------------- office actions

def in_flight_for_service(db: Session, run: ProvisioningRun) -> bool:
    """Another run for `run`'s service is still in flight (any purpose/mode)."""
    return db.execute(
        sa.select(ProvisioningRun.id).where(
            ProvisioningRun.client_service_id == run.client_service_id,
            ProvisioningRun.id != run.id,
            ProvisioningRun.status.in_(IN_FLIGHT),
        ).limit(1)
    ).first() is not None


def _later_run_for_service(db: Session, run: ProvisioningRun) -> bool:
    """A non-dry run opened on the same service after `run`: undoing `run`'s
    devices now would undo that newer state (doc 42 §7.6)."""
    return db.execute(
        sa.select(ProvisioningRun.id).where(
            ProvisioningRun.client_service_id == run.client_service_id,
            ProvisioningRun.id != run.id,
            ProvisioningRun.dry_run.is_(False),
            ProvisioningRun.created_at > run.created_at,
        ).limit(1)
    ).first() is not None


def _service_refusal(db: Session, run: ProvisioningRun) -> Optional[str]:
    if in_flight_for_service(db, run):
        return "another run for this service is in flight"
    if _later_run_for_service(db, run):
        return "a later run exists for this service"
    return None


def revert_refusal(db: Session, run: ProvisioningRun) -> Optional[str]:
    """Why `revert_run` would refuse `run` (409 RUN_NOT_REVERTIBLE reason), or
    None. The ONE guard: revert_run raises from it, and the backend's run
    `actions` (doc 48 §10.3) call it read-only to enable the button."""
    if run.dry_run:
        return "a dry run changed nothing"
    if run.status != ProvisioningJobStatus.SUCCEEDED:
        return "only a SUCCEEDED run can be reverted"
    if run.phase is None:
        return "a legacy run has no rollback snapshot"
    return _service_refusal(db, run)


def rollback_retry_refusal(db: Session, run: ProvisioningRun) -> Optional[str]:
    """Why `retry_rollback` would refuse `run`, or None (see revert_refusal)."""
    if (run.dry_run or run.status != ProvisioningJobStatus.FAILED
            or run.error_code != ROLLBACK_INCOMPLETE or run.phase is None):
        return "only a ROLLBACK_INCOMPLETE run can retry its rollback"
    return _service_refusal(db, run)


def revert_run(db: Session, run: ProvisioningRun) -> Optional[ProvisioningJob]:
    """"Revertir" a SUCCEEDED run (doc 42 §7.6): roll back every device it
    configured, through each playbook's own rollback. Returns the first
    rollback child, or None when the run closed at once (nothing to undo).
    Raises RunNotRevertible (409) unless the run is non-dry, SUCCEEDED, v2,
    with no in-flight run on the service and no later non-dry run (reverting
    an ACTIVATION after a later SUSPENSION would undo the wrong state)."""
    run = _lock_run(db, run.id)
    reason = revert_refusal(db, run)
    if reason:
        raise RunNotRevertible(reason)

    _set_cause(run, REVERTED, None)
    run.status = ProvisioningJobStatus.RUNNING
    run.phase = PHASE_ROLLBACK
    run.finished_at = None
    _, entries, no_rollback = _plan_rollback(db, run)
    _record_rollback(run, entries, no_rollback)
    return _close_after_rollback_planned(db, run, entries, no_rollback)


def retry_rollback(db: Session, run: ProvisioningRun) -> Optional[ProvisioningJob]:
    """"Reintentar reversión" of a ROLLBACK_INCOMPLETE run (doc 42 §7.6): re-run
    the rollback of the devices whose latest ROLLBACK child did not succeed
    (or never ran). error_code goes back to the original cause, so a
    successful retry ends ROLLED_BACK with it. NO_ROLLBACK_DEFINED devices are
    not retried (nothing to run): such a run ends ROLLBACK_INCOMPLETE again.
    Refused, like revert_run, while another run for the service is in flight
    or once a later non-dry run exists (a §7.7 corrective run that succeeded
    must not be undone by retrying the old rollback)."""
    run = _lock_run(db, run.id)
    reason = rollback_retry_refusal(db, run)
    if reason:
        raise RunNotRevertible(reason)

    plan = run.plan or []
    latest: Dict[str, Optional[ProvisioningJob]] = {
        e["item_id"]: None for e in plan if e.get("phase") == PHASE_ROLLBACK}
    for job in _children(db, run):
        if job.run_position < len(plan) and plan[job.run_position].get("phase") == PHASE_ROLLBACK:
            latest[plan[job.run_position]["item_id"]] = job
    items = [i for i, j in latest.items() if j is None or j.status not in TERMINAL_OK]

    state = (run.frames or {}).get("rollback") or {}
    run.error_code = state.get("cause_code") or run.error_code
    run.error = state.get("cause_error")
    run.status = ProvisioningJobStatus.RUNNING
    run.phase = PHASE_ROLLBACK
    run.finished_at = None
    entries = append_rollback(db, run, items=items) if items else []
    if entries:
        return _queue_child(db, run, len(run.plan) - len(entries))
    _finish_rollback(db, run)
    return None


def create_or_get_run(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
    dry_run: bool = False,
    idempotency_key: Optional[str] = None,
    **kw: Any,
) -> Tuple[ProvisioningRun, bool]:
    """create_run, deduped on the run key: returns (run, created).

    An in-flight run with the same key wins and comes back with created=False.
    The INSERT runs in a SAVEPOINT, so losing the race to a concurrent producer
    (uq_provisioning_run_company_idem) rolls back only the savepoint: the
    caller's session stays usable (a workflow execution still commits) and the
    winner's run is returned instead of a 500. Any other error propagates —
    including create_run's ResolutionError (SECRET_SPEC_CONFLICT,
    OUTPUT_KEY_CONFLICT, SECRETS_KEY_UNAVAILABLE), which callers map to 422.

    Re-running (doc 42 §7.7, founder Q10): both idempotency indexes are partial
    over QUEUED/RUNNING/PENDING_INFORM, so once a run is terminal the same key
    opens a NEW run — fresh resolution, snapshot and secrets. A run still in
    ROLLBACK is in flight, so a retry returns it (created=False) and always
    starts from devices that are untouched or restored.
    """
    key = idempotency_key or run_idempotency_key(client_service.id, purpose, dry_run)
    existing = find_in_flight_run(db, client_service.company_id, key)
    if existing is not None:
        return existing, False
    try:
        with db.begin_nested():
            run = create_run(db, client_service, purpose=purpose, dry_run=dry_run,
                             idempotency_key=key, **kw)
    except IntegrityError:
        existing = find_in_flight_run(db, client_service.company_id, key)
        if existing is None:
            raise
        return existing, False
    return run, True


def repair_stranded_runs(db: Session, limit: int = 100) -> int:
    """Backstop: advance in-flight runs that have no in-flight child.

    A run strands when its child reached a terminal state but advance_run did
    not commit with it (the settle isolates it in a savepoint, so a failure
    there no longer rolls back the job's outcome). Each candidate is taken FOR
    UPDATE SKIP LOCKED — a run a settle is advancing right now is skipped — and
    repaired in its own savepoint. Only run rows are locked here, so there is no
    deadlock cycle with a settle (job row, then run row).

    A run whose last child finished more than STRANDED_RUN_MAX_AGE ago is not
    simply advanced (doc 42 §8.4): in PRECONDITIONS, or a legacy / dry run, it
    is closed FAILED/STRANDED_RUN_EXPIRED; in CONFIGURATION / VERIFICATION it
    ENTERS ROLLBACK (the safe direction) — unless its last forward entry
    already succeeded and only the final advance was lost, then it is finished
    SUCCEEDED; in ROLLBACK it keeps advancing, and
    one with nothing left to advance is closed ROLLBACK_INCOMPLETE. Returns the
    number of runs repaired; the caller commits.
    """
    has_live_child = sa.exists().where(
        ProvisioningJob.run_id == ProvisioningRun.id,
        ProvisioningJob.status.in_(IN_FLIGHT),
    )
    runs = db.execute(
        sa.select(ProvisioningRun)
        .where(
            ProvisioningRun.status.in_(IN_FLIGHT),
            ProvisioningRun.updated_at < now_gt() - STRANDED_RUN_GRACE,
            ~has_live_child,
        )
        .order_by(ProvisioningRun.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    repaired = 0
    for run in runs:
        run_id = run.id
        try:
            with db.begin_nested():
                last = db.execute(
                    sa.select(ProvisioningJob)
                    .where(ProvisioningJob.run_id == run_id)
                    .order_by(ProvisioningJob.run_position.desc())
                    .limit(1)
                ).scalars().first()
                if last is not None and last.status in IN_FLIGHT:
                    continue
                quiet_since = (last.finished_at if last is not None else None) or run.updated_at
                expired = make_aware_gt(quiet_since) < now_gt() - STRANDED_RUN_MAX_AGE
                if expired and run.phase == PHASE_ROLLBACK and not run.dry_run:
                    if last is None:
                        close_run(db, run, ProvisioningJobStatus.FAILED, ROLLBACK_INCOMPLETE)
                    else:
                        advance_run(db, last)
                elif (expired and not run.dry_run and last is not None
                      and last.status in TERMINAL_OK
                      and last.run_position == len(run.plan or []) - 1):
                    # Only the final advance was lost: nothing would be
                    # configured forward, so finishing carries no stale-plan risk.
                    advance_run(db, last)
                elif (expired and not run.dry_run
                      and run.phase in (PHASE_CONFIGURATION, PHASE_VERIFICATION)):
                    _enter_rollback(db, run, STRANDED_RUN_EXPIRED,
                                    "La ejecución quedó detenida más de 1 h")
                elif expired:
                    close_run(db, run, ProvisioningJobStatus.FAILED, STRANDED_RUN_EXPIRED)
                    logger.bind(run_id=str(run_id)).warning(
                        "repair_stranded_runs: STRANDED_RUN_EXPIRED, run closed FAILED")
                elif last is None:
                    if _queue_child(db, run, 0) is None:
                        # An empty plan has nothing to configure.
                        close_run(db, run, ProvisioningJobStatus.SUCCEEDED)
                else:
                    advance_run(db, last)
            repaired += 1
        except Exception:
            logger.bind(run_id=str(run_id)).exception("repair_stranded_runs: run not repaired")
    return repaired
