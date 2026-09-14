# utils/workflow_engine.py
"""
Workflow engine: trigger matching and asynchronous DAG execution.

Called from CRUD routers after audit logging to fire matching workflows.
Workflows execute asynchronously via asyncio.create_task().
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from collections import defaultdict, deque
from typing import Optional, List, Dict, Any
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session, joinedload

from database_utils.database import SessionLocal
from database_utils.models.workflow import (
    Workflow,
    WorkflowTrigger,
    WorkflowStep,
    WorkflowExecution,
    WorkflowStepExecution,
    StepActionType,
    ExecutionStatus,
)
from database_utils.utils.timezone_utils import now_gt


# Recursion guard to prevent infinite trigger chains
_workflow_execution_depth = contextvars.ContextVar('workflow_depth', default=0)
MAX_WORKFLOW_DEPTH = 3

# Strong references to fire-and-forget workflow tasks. The event loop only keeps
# a weak reference to a Task, so without this a scheduled workflow execution can
# be garbage-collected mid-run (WF-3). Tasks discard themselves on completion.
_background_tasks: set[asyncio.Task] = set()

# Map resource types to their SQLAlchemy model classes
_MODEL_MAP = None

# Static mirror of _get_model_map()'s keys (Cycle 2, doc 18 §5/D10):
# importable WITHOUT triggering the lazy model load (avoids circular imports
# for callers that only need the resource-type universe, e.g. isp_seed.py's
# template trigger-resource-type gate and the workflow trigger 422 validation
# on WorkflowTriggerCreate/Update). Kept in sync with _get_model_map() by
# hand — there is no dynamic way to do this without importing the models.
# 'network_node' REMOVED (Cycle 2 D6, revision c2d_graph_removal).
KNOWN_RESOURCE_TYPES = frozenset({
    "order", "client", "product", "task", "task_state",
    "recurring_order", "invoice", "order_item",
    "service_plan", "client_service", "service_suspension",
    "inventory_item", "provisioning_job",
})


def _get_model_map():
    """Lazy-load model map to avoid circular imports."""
    global _MODEL_MAP
    if _MODEL_MAP is None:
        from database_utils.models.crm import (
            Order, Client, Product, Task, TaskState,
            RecurringOrder, Invoice, OrderItem,
        )
        from database_utils.models.isp import (
            ServicePlan, ClientService, ServiceSuspension,
            InventoryItem, ProvisioningJob,
        )
        _MODEL_MAP = {
            "order": Order,
            "client": Client,
            "product": Product,
            "task": Task,
            "task_state": TaskState,
            "recurring_order": RecurringOrder,
            "invoice": Invoice,
            "order_item": OrderItem,
            # ISP resources
            "service_plan": ServicePlan,
            "client_service": ClientService,
            "service_suspension": ServiceSuspension,
            "inventory_item": InventoryItem,
            # 'network_node' REMOVED (Cycle 2 D6, revision c2d_graph_removal —
            # the free-form network graph no longer exists).
            "provisioning_job": ProvisioningJob,
        }
        assert set(_MODEL_MAP) == set(KNOWN_RESOURCE_TYPES), (
            "KNOWN_RESOURCE_TYPES drifted from _get_model_map() — update both"
        )
    return _MODEL_MAP


async def check_workflow_triggers(
    db: Session,
    company_id: UUID,
    resource_type: str,
    event_type: str,
    resource_id: UUID,
    before_data: Optional[dict] = None,
    after_data: Optional[dict] = None,
) -> None:
    """
    Called after each CRUD operation. Finds matching active workflows
    for this company and fires them asynchronously.

    Args:
        db: Current request's database session (used only for querying workflows)
        company_id: The company that owns the resource
        resource_type: e.g. "order", "client", "product", "task"
        event_type: "CREATED", "UPDATED", "DELETED"
        resource_id: The ID of the affected resource
        before_data: State before the change (for updates/deletes)
        after_data: State after the change (for creates/updates)
    """
    depth = _workflow_execution_depth.get()
    if depth >= MAX_WORKFLOW_DEPTH:
        logger.warning(
            f"Workflow recursion depth {depth} exceeded for {resource_type}.{event_type}, skipping"
        )
        return

    try:
        matched = find_matching_workflows(
            db, company_id, resource_type, event_type, before_data, after_data
        )

        if not matched:
            return

        logger.info(
            f"Found {len(matched)} matching workflow(s) for "
            f"{resource_type}.{event_type} on resource {resource_id}"
        )

        from database_utils.utils.audit_utils import serialize_for_audit

        trigger_event = serialize_for_audit({
            "resource_type": resource_type,
            "event_type": event_type,
            "resource_id": str(resource_id),
            "before": before_data,
            "after": after_data,
        })

        for workflow in matched:
            task = asyncio.create_task(
                _execute_workflow_async(
                    workflow_id=workflow.id,
                    trigger_event=trigger_event,
                    company_id=company_id,
                    depth=depth,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    except Exception as e:
        logger.error(f"Error checking workflow triggers: {e}")


def find_matching_workflows(
    db: Session,
    company_id: UUID,
    resource_type: str,
    event_type: str,
    before_data: Optional[dict],
    after_data: Optional[dict],
) -> List[Workflow]:
    """
    Query active workflows for this company that have triggers matching
    the resource_type + event_type + field_conditions.
    """
    workflows = (
        db.query(Workflow)
        .join(WorkflowTrigger)
        .filter(
            Workflow.company_id == company_id,
            Workflow.is_active == True,
            WorkflowTrigger.resource_type == resource_type,
            WorkflowTrigger.event_type == event_type,
        )
        .options(joinedload(Workflow.triggers))
        .all()
    )

    # The filter join on WorkflowTrigger fans out one row per matching trigger,
    # so `workflows` can contain the same Workflow multiple times. Dedupe by id
    # so a workflow with >1 matching trigger executes exactly once (WF-2).
    matched = []
    seen: set = set()
    for workflow in workflows:
        if workflow.id in seen:
            continue
        for trigger in workflow.triggers:
            if (
                trigger.resource_type == resource_type
                and trigger.event_type.value == event_type
                and _matches_field_conditions(trigger.field_conditions, before_data, after_data)
            ):
                matched.append(workflow)
                seen.add(workflow.id)
                break

    return matched


def _matches_field_conditions(
    conditions: Optional[dict],
    before_data: Optional[dict],
    after_data: Optional[dict],
) -> bool:
    """
    Evaluate whether the trigger's field_conditions match the actual change.
    If conditions is None, any event of the right type matches.
    """
    if conditions is None:
        return True

    field = conditions.get("field")
    operator = conditions.get("operator")
    value = conditions.get("value")

    if not field or not operator:
        return True

    def _norm(v) -> str:
        # Enum members stringify as "ClassName.MEMBER"; compare by value.
        import enum as _enum
        if isinstance(v, _enum.Enum):
            v = v.value
        return str(v)

    if operator == "changed":
        if before_data and after_data:
            return _norm(before_data.get(field)) != _norm(after_data.get(field))
        return True

    elif operator == "changed_to":
        # True only on the transition INTO `value` — the field must not have
        # already equalled `value` before (WF-1). Missing before_data (e.g. on
        # CREATE) counts as "did not equal it before".
        if not after_data:
            return False
        before_val = _norm(before_data.get(field)) if before_data else None
        return before_val != _norm(value) and _norm(after_data.get(field)) == _norm(value)

    elif operator == "changed_from":
        # True only on the transition AWAY from `value`.
        if not before_data:
            return False
        after_val = _norm(after_data.get(field)) if after_data else None
        return _norm(before_data.get(field)) == _norm(value) and after_val != _norm(value)

    elif operator == "equals":
        if after_data:
            return _norm(after_data.get(field)) == _norm(value)
        return False

    return True


async def _execute_workflow_async(
    workflow_id: UUID,
    trigger_event: dict,
    company_id: UUID,
    depth: int,
) -> None:
    """
    Async wrapper that creates its own DB session and executes the workflow.
    Runs as a fire-and-forget background task.
    """
    _workflow_execution_depth.set(depth + 1)
    db = SessionLocal()
    try:
        workflow = (
            db.query(Workflow)
            .options(
                joinedload(Workflow.steps),
                joinedload(Workflow.edges),
            )
            .filter(Workflow.id == workflow_id)
            .first()
        )

        if not workflow:
            logger.error(f"Workflow {workflow_id} not found for execution")
            return

        execute_workflow(db, workflow, trigger_event, company_id)

    except Exception as e:
        logger.error(f"Workflow {workflow_id} execution failed: {e}")
        db.rollback()
    finally:
        db.close()
        _workflow_execution_depth.set(depth)


def execute_workflow(
    db: Session,
    workflow: Workflow,
    trigger_event: dict,
    company_id: UUID,
) -> WorkflowExecution:
    """Execute all steps of a workflow in topological order (Kahn's algorithm)."""

    execution = WorkflowExecution(
        workflow_id=workflow.id,
        trigger_event=trigger_event,
        status=ExecutionStatus.RUNNING,
        started_at=now_gt(),
    )
    db.add(execution)
    db.flush()

    steps_by_id = {step.id: step for step in workflow.steps}

    if not steps_by_id:
        execution.status = ExecutionStatus.COMPLETED
        execution.completed_at = now_gt()
        db.commit()
        return execution

    # Build adjacency list and in-degree count
    in_degree: Dict[UUID, int] = {step_id: 0 for step_id in steps_by_id}
    adjacency: Dict[UUID, List[UUID]] = {step_id: [] for step_id in steps_by_id}

    for edge in workflow.edges:
        if edge.from_step_id in adjacency and edge.to_step_id in in_degree:
            adjacency[edge.from_step_id].append(edge.to_step_id)
            in_degree[edge.to_step_id] += 1

    # Kahn's algorithm: start with nodes that have no incoming edges
    queue = deque([sid for sid, deg in in_degree.items() if deg == 0])
    execution_context: Dict[str, Any] = {"trigger": trigger_event}
    processed_count = 0

    try:
        while queue:
            step_id = queue.popleft()
            processed_count += 1
            step = steps_by_id[step_id]

            step_execution = WorkflowStepExecution(
                execution_id=execution.id,
                step_id=step_id,
                # Snapshot (revision c2e_step_exec_snapshot): step_id is now
                # nullable/SET NULL, so historical run views must be able to
                # render from step_name alone once the step itself is deleted
                # or renamed.
                step_name=step.name,
                status=ExecutionStatus.RUNNING,
                started_at=now_gt(),
            )
            db.add(step_execution)
            db.flush()

            try:
                result = execute_step(db, step, execution_context, company_id)
                step_execution.status = ExecutionStatus.COMPLETED
                step_execution.result = result
                step_execution.completed_at = now_gt()
                execution_context[str(step_id)] = result
            except Exception as e:
                step_execution.status = ExecutionStatus.FAILED
                step_execution.error = str(e)
                step_execution.completed_at = now_gt()
                raise

            # Reduce in-degree for neighbors
            for neighbor_id in adjacency[step_id]:
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    queue.append(neighbor_id)

        # WF-4: if Kahn's algorithm didn't reach every step, the graph has a
        # cycle (or unreachable steps). Fail explicitly instead of falling
        # through to COMPLETED with a partially-executed graph.
        if processed_count < len(steps_by_id):
            raise ValueError(
                f"Workflow graph has a cycle or unreachable steps: "
                f"{processed_count}/{len(steps_by_id)} steps executed"
            )

        execution.status = ExecutionStatus.COMPLETED
        execution.completed_at = now_gt()

    except Exception as e:
        execution.status = ExecutionStatus.FAILED
        execution.error = str(e)
        execution.completed_at = now_gt()
        logger.error(f"Workflow execution {execution.id} failed: {e}")

    db.commit()

    # Follow-on CREATED triggers queued by CREATE_ORDER/CREATE_TASK fire only
    # now, after the commit above — a follow-on workflow's fresh session is
    # guaranteed to see the created entities. Steps that completed before a
    # later step failed are committed (existing semantics), so their queued
    # triggers fire too.
    for pending in execution_context.get(_PENDING_TRIGGERS_KEY, []):
        _fire_created_trigger(*pending)

    return execution


def execute_step(
    db: Session,
    step: WorkflowStep,
    context: dict,
    company_id: UUID,
) -> dict:
    """Execute a single workflow step based on its action_type."""
    # Resolve {{trigger.*}} / {{steps.*}} templates across the whole config so
    # dynamic resource_ids and entity data work in every action type.
    # (HTTP_REQUEST re-resolves its body internally — idempotent.)
    config = _resolve_template(step.action_config, context)

    if step.action_type == StepActionType.UPDATE_FIELD:
        return _execute_update_field(db, config, context, company_id)
    elif step.action_type == StepActionType.CREATE_ENTITY:
        return _execute_create_entity(db, config, context, company_id)
    elif step.action_type == StepActionType.HTTP_REQUEST:
        return _execute_http_request(db, step, context, company_id)
    elif step.action_type == StepActionType.ENQUEUE_PROVISIONING:
        return _execute_enqueue_provisioning(db, config, context, company_id)
    elif step.action_type == StepActionType.CREATE_ORDER:
        return _execute_create_order(db, config, context, company_id)
    elif step.action_type == StepActionType.CREATE_TASK:
        return _execute_create_task(db, config, context, company_id)
    else:
        raise ValueError(f"Unsupported action type: {step.action_type}")


# Single-writer invariant (doc 16 §5.3/§6.6, MAJOR fix): payment/money state on
# orders is written ONLY by backend-erp's PaymentService. Tenant automations
# must never mutate these via UPDATE_FIELD — the step fails with an explicit
# error instead of silently corrupting billing state. payment_status/order_type
# remain readable in trigger conditions (evaluation uses before/after dicts).
UPDATE_FIELD_DENYLIST: Dict[str, frozenset] = {
    "order": frozenset({"paid", "payment_status", "payment_date", "total", "total_cents"}),
    # Invoices are created/invalidated ONLY by PaymentService (doc 16 §1):
    # automations must not flip validity or rewrite invoice money.
    "invoice": frozenset(
        {"is_valid", "subtotal", "tax", "total",
         "subtotal_cents", "tax_cents", "total_cents"}
    ),
    # Cycle 2 (doc 18 amendment 1): migration-critical bridge state — the
    # rewritten suspension/reactivation/service-removal templates (and any
    # future automation) may freely write billing_status/recurrence/
    # recurrence_end/next_generation_date/last_generated_at/quantity, but
    # migration_source and recurring_order_id are NOT payment-ledger data —
    # they are the c2b rollback bridge, and UPDATE_FIELD bypasses the
    # ClientServiceUpdate schema (which already excludes both) via plain
    # setattr/hasattr, so the engine must deny them independently.
    "client_service": frozenset({"migration_source", "recurring_order_id"}),
    # Same rationale for the ServicePlan migration marker.
    "service_plan": frozenset({"migration_source"}),
}

# CREATE_ENTITY guards for the same invariant: the engine must never create
# invoices at all, and orders it creates must never be born with payment/money
# state (that is PaymentService's exclusive domain).
CREATE_ENTITY_FORBIDDEN_TYPES: frozenset = frozenset({"invoice"})
CREATE_ENTITY_FIELD_DENYLIST: Dict[str, frozenset] = {
    "order": frozenset({"paid", "payment_status", "payment_date", "total", "total_cents"}),
    "client_service": frozenset({"migration_source", "recurring_order_id"}),
    "service_plan": frozenset({"migration_source"}),
}


def _execute_update_field(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """Update fields on a target entity."""
    model_map = _get_model_map()
    resource_type = config.get("resource_type")
    if not resource_type or resource_type not in model_map:
        raise ValueError(f"Unknown resource_type: {resource_type}")

    model_class = model_map[resource_type]

    updates = config.get("updates", {})

    denied = sorted(set(updates) & UPDATE_FIELD_DENYLIST.get(resource_type, frozenset()))
    if denied:
        raise ValueError(
            f"UPDATE_FIELD may not write protected field(s) {', '.join(denied)} "
            f"on '{resource_type}': payment/money state has a single writer "
            f"(PaymentService)"
        )

    # Determine which resource(s) to update
    resource_id_source = config.get("resource_id_source", "trigger")

    if resource_id_source == "match_field":
        # Match by relationship: update ALL records where match_field == trigger resource_id
        match_field = config.get("match_field")
        if not match_field:
            raise ValueError("match_field is required when resource_id_source is 'match_field'")

        trigger_resource_id = context["trigger"]["resource_id"]
        if not hasattr(model_class, match_field):
            raise ValueError(f"{resource_type} has no field '{match_field}'")

        entities = db.query(model_class).filter(
            getattr(model_class, match_field) == trigger_resource_id,
            model_class.company_id == company_id,
        ).all()

        updated_ids = []
        updated_fields = []
        for entity in entities:
            for field, value in updates.items():
                if hasattr(entity, field):
                    setattr(entity, field, value)
                    if field not in updated_fields:
                        updated_fields.append(field)
            updated_ids.append(str(entity.id))

        db.flush()
        return {
            "updated_fields": updated_fields,
            "updated_count": len(entities),
            "updated_ids": updated_ids,
            "match_field": match_field,
        }

    else:
        # Single-record mode: trigger or custom resource_id
        if resource_id_source == "trigger":
            resource_id = context["trigger"]["resource_id"]
        else:
            resource_id = config.get("resource_id")

        if not resource_id:
            raise ValueError("No resource_id resolved for UPDATE_FIELD step")

        entity = db.query(model_class).filter(
            model_class.id == resource_id,
            model_class.company_id == company_id,
        ).first()

        if not entity:
            raise ValueError(f"{resource_type} {resource_id} not found")

        updated_fields = []
        for field, value in updates.items():
            if hasattr(entity, field):
                setattr(entity, field, value)
                updated_fields.append(field)

        db.flush()
        return {"updated_fields": updated_fields, "resource_id": str(resource_id)}


def _execute_create_entity(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """Create a new entity."""
    model_map = _get_model_map()
    resource_type = config.get("resource_type")
    if not resource_type or resource_type not in model_map:
        raise ValueError(f"Unknown resource_type: {resource_type}")

    if resource_type in CREATE_ENTITY_FORBIDDEN_TYPES:
        raise ValueError(
            f"CREATE_ENTITY may not create '{resource_type}': invoices are "
            f"created/invalidated only by PaymentService (single-writer invariant)"
        )

    model_class = model_map[resource_type]
    data = dict(config.get("data", {}))

    denied = sorted(set(data) & CREATE_ENTITY_FIELD_DENYLIST.get(resource_type, frozenset()))
    if denied:
        raise ValueError(
            f"CREATE_ENTITY may not set protected field(s) {', '.join(denied)} "
            f"on '{resource_type}': payment/money state has a single writer "
            f"(PaymentService)"
        )

    data["company_id"] = company_id

    entity = model_class(**data)
    db.add(entity)
    db.flush()

    return {"created_resource_type": resource_type, "resource_id": str(entity.id)}


def _required_uuid(value: Any, field: str, action: str) -> UUID:
    """Parse a config value into a UUID or fail the step with a clear error
    (an unresolved '{{...}}' template or empty string lands here)."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise ValueError(f"{action}: '{field}' did not resolve to a UUID (got {value!r})")


# execution_context key holding (company_id, resource_type, resource_id,
# after_data) tuples queued by CREATE_ORDER/CREATE_TASK. Cannot collide with
# step results (stored under str(step_uuid)) or "trigger".
_PENDING_TRIGGERS_KEY = "_pending_created_triggers"


def _queue_created_trigger(
    context: dict,
    company_id: UUID,
    resource_type: str,
    resource_id: UUID,
    after_data: dict,
) -> None:
    """
    Queue a follow-on CREATED trigger for an entity created inside a running
    step (doc 16 §5.2). execute_workflow schedules the queued triggers AFTER
    its db.commit(), guaranteeing the follow-on workflow's fresh session sees
    the committed entity (and that nothing fires for a run whose commit
    fails). Scheduling directly from the step handler would rely on the
    engine's call graph staying await-free between handler and commit.
    """
    context.setdefault(_PENDING_TRIGGERS_KEY, []).append(
        (company_id, resource_type, resource_id, after_data)
    )


def _fire_created_trigger(
    company_id: UUID,
    resource_type: str,
    resource_id: UUID,
    after_data: dict,
) -> bool:
    """
    Fire follow-on CREATED triggers for a committed entity. Called by
    execute_workflow after its db.commit() (see _queue_created_trigger).
    execute_workflow is sync, so the check is scheduled on the running event
    loop; asyncio.create_task copies the current context, so the depth
    contextvar (already depth+1 inside this execution) still enforces
    MAX_WORKFLOW_DEPTH. The scheduled coroutine opens its own session.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            f"No running event loop; skipping {resource_type}.CREATED workflow "
            f"triggers for {resource_id}"
        )
        return False

    async def _fire() -> None:
        session = SessionLocal()
        try:
            await check_workflow_triggers(
                db=session,
                company_id=company_id,
                resource_type=resource_type,
                event_type="CREATED",
                resource_id=resource_id,
                before_data=None,
                after_data=after_data,
            )
        finally:
            session.close()

    task = loop.create_task(_fire())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return True


def _execute_create_order(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """
    CREATE_ORDER action (doc 16 §5.2, installation flow; doc 18 D1/D2/
    amendment 9, Cycle 2). Mirrors backend-erp orders.py:create_order.

    action_config format (templates already resolved by execute_step):
    {
      "order_type": "INSTALLATION",        # optional explicit signal, see below
      "client_id": "<uuid>",               # {{trigger.after.client_id}}
      "client_service_id": "<uuid>",       # {{trigger.resource_id}}
      "items": [{"service_plan_id": "<uuid>", "quantity": 1}],   # preferred
      # or (deprecated, dual-write rollback window):
      # "items": [{"product_id": "<uuid>", "quantity": 1}],
      "due_date_offset_days": 0,
      "idempotency_key": "install-order-<uuid>"   # informational; dedupe below
    }

    Single-writer invariant (§5.3/§6.6): orders created by the engine ALWAYS
    start payment_status=PENDING / paid=False. The engine never writes payment
    or money state after creation — that is PaymentService's job.

    Cycle 2 item resolution (D1): each item resolves against EXACTLY ONE of
    service_plan_id (preferred — price comes from service_plan.price_cents)
    or product_id (deprecated but still honored during the rollback window —
    price comes from product.price_cents, and its kind for order_type
    derivation is resolved via the service_plan.product_id bridge; a
    bridge-less legacy product counts as SERVICE, doc 18 §1c). order_item
    rows dual-write both FKs when a bridge exists.

    order_type is DERIVED (utils/order_typing.derive_order_type, amendment 9)
    from the resolved items' CatalogKind, honoring this step's configured
    order_type as an additional input (never silently dropped) — this is what
    keeps the installed v2 new-installation template's INSTALLATION dedupe/DB
    backstop working even when its configured fee catalog item doesn't itself
    resolve to an INSTALLATION-kind plan/product.

    Idempotency for INSTALLATION orders: (1) precheck — an existing
    non-cancelled INSTALLATION order for the client_service dedupes the step;
    (2) partial unique index uq_order_installation_per_service (revision
    c1e_install_actions) backs it at the DB level, so a genuine race fails the
    step instead of double-billing.
    """
    from datetime import timedelta
    from decimal import Decimal, ROUND_HALF_UP
    from database_utils.models.crm import (
        Client, Order, OrderItem, OrderStatus, OrderType, PaymentStatus, Product,
    )
    from database_utils.models.isp import ClientService, ServicePlan, CatalogKind
    from database_utils.utils.audit_utils import serialize_for_audit
    from database_utils.utils.order_typing import derive_order_type

    def _cents_from_float(amount) -> int:
        # Dual-window fallback (row written before its price_cents backfill).
        # Half-away-from-zero to match the canonical
        # ROUND(x::numeric*100)::bigint rule (doc 16 §2.5) — Python's round()
        # is banker's rounding and would diverge.
        return int((Decimal(str(amount or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _kind_value(kind) -> str:
        return kind.value if hasattr(kind, "value") else str(kind)

    explicit_order_type = config.get("order_type")

    # --- Company-scoped target resolution (config is tenant-editable: never
    # trust a raw id — every referenced row must belong to THIS company) ---
    client_service = None
    client_service_id = None
    if config.get("client_service_id") or explicit_order_type == OrderType.INSTALLATION.value:
        client_service_id = _required_uuid(
            config.get("client_service_id"), "client_service_id", "CREATE_ORDER"
        )
        client_service = db.query(ClientService).filter(
            ClientService.id == client_service_id,
            ClientService.company_id == company_id,
        ).first()
        if not client_service:
            raise ValueError(
                f"CREATE_ORDER: client_service {client_service_id} not found "
                f"for company {company_id}"
            )

    if config.get("client_id"):
        client_id = _required_uuid(config.get("client_id"), "client_id", "CREATE_ORDER")
        owned = db.query(Client.id).filter(
            Client.id == client_id, Client.company_id == company_id
        ).first()
        if not owned:
            raise ValueError(
                f"CREATE_ORDER: client {client_id} not found for company {company_id}"
            )
    elif client_service is not None:
        client_id = client_service.client_id
    else:
        client_id = None

    # --- Items: company-scoped plans/products, snapshots, totals in cents,
    # and the resolved kinds that feed order_type derivation ---
    items = config.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("CREATE_ORDER: 'items' must be a non-empty list")

    plan_ids = [
        _required_uuid(item["service_plan_id"], "items[].service_plan_id", "CREATE_ORDER")
        for item in items if item.get("service_plan_id")
    ]
    product_ids = [
        _required_uuid(item["product_id"], "items[].product_id", "CREATE_ORDER")
        for item in items if not item.get("service_plan_id") and item.get("product_id")
    ]
    if len(plan_ids) + len(product_ids) != len(items):
        raise ValueError(
            "CREATE_ORDER: every item needs exactly one of service_plan_id or product_id"
        )

    plan_map = {}
    if plan_ids:
        plans = db.query(ServicePlan).filter(
            ServicePlan.id.in_(plan_ids), ServicePlan.company_id == company_id
        ).all()
        plan_map = {p.id: p for p in plans}
        missing_plans = [str(pid) for pid in plan_ids if pid not in plan_map]
        if missing_plans:
            raise ValueError(
                f"CREATE_ORDER: service_plan(s) not found for company: {', '.join(missing_plans)}"
            )

    product_map = {}
    bridged_plans_by_product = {}
    if product_ids:
        products = db.query(Product).filter(
            Product.id.in_(product_ids), Product.company_id == company_id
        ).all()
        product_map = {p.id: p for p in products}
        missing_products = [str(pid) for pid in product_ids if pid not in product_map]
        if missing_products:
            raise ValueError(
                f"CREATE_ORDER: product(s) not found for company: {', '.join(missing_products)}"
            )
        # Deprecated-path kind resolution via the c2a billing bridge;
        # bridge-less legacy products count as SERVICE (doc 18 §1c).
        bridged = db.query(ServicePlan).filter(
            ServicePlan.product_id.in_(product_ids), ServicePlan.company_id == company_id
        ).all()
        bridged_plans_by_product = {p.product_id: p for p in bridged}

    total_cents = 0
    order_items = []
    item_kinds: List[str] = []
    for item in items:
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError(f"CREATE_ORDER: invalid quantity {item.get('quantity')!r}")
        if quantity <= 0:
            raise ValueError("CREATE_ORDER: item quantity must be > 0")

        if item.get("service_plan_id"):
            plan = plan_map[_required_uuid(item["service_plan_id"], "items[].service_plan_id", "CREATE_ORDER")]
            unit_price_cents = plan.price_cents if plan.price_cents is not None else _cents_from_float(plan.price)
            item_kinds.append(_kind_value(plan.kind))
            total_cents += unit_price_cents * quantity
            order_items.append(OrderItem(
                service_plan_id=plan.id,
                product_id=plan.product_id,  # dual-write, rollback window
                quantity=quantity,
                # §2.2 snapshots: app-level required for all new rows
                unit_price_cents=unit_price_cents,
                product_name=plan.name,
            ))
        else:
            product = product_map[_required_uuid(item["product_id"], "items[].product_id", "CREATE_ORDER")]
            unit_price_cents = product.price_cents if product.price_cents is not None else _cents_from_float(product.price)
            bridged_plan = bridged_plans_by_product.get(product.id)
            item_kinds.append(_kind_value(bridged_plan.kind) if bridged_plan else CatalogKind.SERVICE.value)
            total_cents += unit_price_cents * quantity
            order_items.append(OrderItem(
                product_id=product.id,
                service_plan_id=bridged_plan.id if bridged_plan else None,
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                product_name=product.name,
            ))

    order_type = derive_order_type(item_kinds, explicit_order_type=explicit_order_type)

    # --- Idempotency precheck (§5.2): one live INSTALLATION order per service ---
    if order_type == OrderType.INSTALLATION:
        if client_service_id is None:
            raise ValueError("CREATE_ORDER: INSTALLATION orders require client_service_id")
        existing = db.query(Order).filter(
            Order.company_id == company_id,
            Order.client_service_id == client_service_id,
            Order.order_type == OrderType.INSTALLATION,
            Order.status != OrderStatus.CANCELLED,
        ).first()
        if existing:
            return {
                "deduped": True,
                "created_resource_type": "order",
                "resource_id": str(existing.id),
            }

    try:
        offset_days = int(config.get("due_date_offset_days") or 0)
    except (TypeError, ValueError):
        raise ValueError(
            f"CREATE_ORDER: invalid due_date_offset_days "
            f"{config.get('due_date_offset_days')!r}"
        )

    order = Order(
        company_id=company_id,
        client_id=client_id,
        client_service_id=client_service_id,
        order_type=order_type,
        payment_status=PaymentStatus.PENDING,  # engine never writes payment state
        paid=False,                            # dual-write kept through Cycle 1
        status=OrderStatus.ACTIVE,
        total=total_cents / 100,               # Float dual-write through Cycle 1
        total_cents=total_cents,
        due_date=now_gt() + timedelta(days=offset_days),
    )
    order.order_items = order_items
    db.add(order)
    db.flush()

    after_data = serialize_for_audit(
        {c.name: getattr(order, c.name) for c in order.__table__.columns}
    )
    _queue_created_trigger(context, company_id, "order", order.id, after_data)

    return {
        "deduped": False,
        "created_resource_type": "order",
        "resource_id": str(order.id),
        "order_type": order_type.value,
        "total_cents": total_cents,
    }


def _execute_create_task(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """
    CREATE_TASK action (doc 16 §5.2, installation flow). Replicates
    backend-erp tasks.py:create_task invariants: company-scoped state
    validation, position = max(position)+1 in the target column,
    company-scoped assignees, created_by=NULL (system-created), fires
    task CREATED triggers.

    action_config format (templates already resolved):
    {
      "name": "...",
      "description": "...",                      # may embed {{steps.s1.resource_id}}
      "task_state_id": "<uuid>",                 # {{param:install_state_id}}
      "linked_object_type": "CLIENT_SERVICE",    # Cycle-3 join key — ORDER linkage
      "linked_object_id": "<uuid>",              #   is forbidden for installs (§5.2)
      "assignee_source": "client_technician" | "fixed" | "none",   # default "none"
      "assignee_ids": ["<uuid>", ...],           # for "fixed"; also the fallback
                                                 #   list for "client_technician"
      "client_id": "<uuid>",                     # for "client_technician"
      "due_date_offset_days": 3                  # optional
    }

    Assignee resolution for "client_technician": the client's
    assigned_technician_id; if the client has none, fall back to the
    (optional) fixed assignee_ids list; else the task is left unassigned for
    the dispatcher (round-robin rejected for Cycle 1 — assignee_source keeps
    it schema-free later).
    """
    from datetime import timedelta
    from sqlalchemy import func
    from database_utils.models.auth import User
    from database_utils.models.crm import Client, Task, TaskLinkedObjectType, TaskState
    from database_utils.models.isp import ClientService
    from database_utils.utils.audit_utils import serialize_for_audit

    name = config.get("name")
    if not name or not str(name).strip():
        raise ValueError("CREATE_TASK: 'name' is required")

    # --- Company-scoped state validation (tasks.py:create_task pattern) ---
    task_state_id = _required_uuid(config.get("task_state_id"), "task_state_id", "CREATE_TASK")
    state = db.query(TaskState).filter(
        TaskState.id == task_state_id, TaskState.company_id == company_id
    ).first()
    if not state:
        raise ValueError(
            f"CREATE_TASK: task_state {task_state_id} not found for company {company_id}"
        )

    # --- Linked object ---
    linked_object_type = None
    linked_object_id = None
    raw_linked_type = config.get("linked_object_type")
    if raw_linked_type:
        try:
            linked_object_type = TaskLinkedObjectType(raw_linked_type)
        except ValueError:
            raise ValueError(
                f"CREATE_TASK: invalid linked_object_type {raw_linked_type!r}"
            )
        linked_object_id = _required_uuid(
            config.get("linked_object_id"), "linked_object_id", "CREATE_TASK"
        )

    # --- Position: end of the target column ---
    max_pos = db.query(func.max(Task.position)).filter(
        Task.task_state_id == task_state_id, Task.company_id == company_id
    ).scalar()
    position = (max_pos + 1) if max_pos is not None else 0

    # --- Assignee resolution ---
    assignee_source = config.get("assignee_source") or "none"
    if assignee_source not in ("client_technician", "fixed", "none"):
        raise ValueError(
            f"CREATE_TASK: invalid assignee_source {assignee_source!r} "
            f"(expected client_technician | fixed | none)"
        )

    # assignee_ids may arrive as an unresolved '{{param:...}}' string when the
    # optional users param was not provided at install time — only a real list
    # of parseable UUIDs counts.
    raw_fixed = config.get("assignee_ids")
    fixed_ids: List[UUID] = []
    if isinstance(raw_fixed, list):
        for value in raw_fixed:
            try:
                fixed_ids.append(UUID(str(value)))
            except (ValueError, TypeError):
                continue

    wanted_ids: List[UUID] = []
    if assignee_source == "client_technician":
        client_id = None
        if config.get("client_id"):
            try:
                client_id = UUID(str(config["client_id"]))
            except (ValueError, TypeError):
                client_id = None
        if (
            client_id is None
            and linked_object_type == TaskLinkedObjectType.CLIENT_SERVICE
            and linked_object_id is not None
        ):
            client_service = db.query(ClientService).filter(
                ClientService.id == linked_object_id,
                ClientService.company_id == company_id,
            ).first()
            client_id = client_service.client_id if client_service else None
        technician_id = None
        if client_id is not None:
            client = db.query(Client).filter(
                Client.id == client_id, Client.company_id == company_id
            ).first()
            technician_id = client.assigned_technician_id if client else None
        # Technician first; fixed list as fallback; else unassigned (dispatcher).
        wanted_ids = [technician_id] if technician_id else fixed_ids
    elif assignee_source == "fixed":
        wanted_ids = fixed_ids

    assignees = []
    if wanted_ids:
        assignees = db.query(User).filter(
            User.id.in_(wanted_ids), User.company_id == company_id
        ).all()

    # --- Optional due date ---
    due_date = None
    if config.get("due_date_offset_days") is not None:
        try:
            due_date = now_gt() + timedelta(days=int(config["due_date_offset_days"]))
        except (TypeError, ValueError):
            raise ValueError(
                f"CREATE_TASK: invalid due_date_offset_days "
                f"{config.get('due_date_offset_days')!r}"
            )

    task = Task(
        name=str(name),
        description=config.get("description"),
        position=position,
        due_date=due_date,
        linked_object_type=linked_object_type,
        linked_object_id=linked_object_id,
        company_id=company_id,
        task_state_id=task_state_id,
        created_by=None,  # system-created (workflow engine), not a user
    )
    if assignees:
        task.assignees = assignees
    db.add(task)
    db.flush()

    after_data = serialize_for_audit(
        {c.name: getattr(task, c.name) for c in task.__table__.columns}
    )
    _queue_created_trigger(context, company_id, "task", task.id, after_data)

    return {
        "created_resource_type": "task",
        "resource_id": str(task.id),
        "task_state_id": str(task_state_id),
        "assignee_ids": [str(u.id) for u in assignees],
    }


def _uuid_or_none(value):
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _owned_or_none(db: Session, model, value, company_id: UUID):
    """Every target FK must belong to THIS company. action_config is
    tenant-editable (a crafted step could name another tenant's integration_id
    to make the worker execute with their stored device credentials), so scope
    each id to company_id and fail the step if a referenced row isn't ours."""
    rid = _uuid_or_none(value)
    if rid is None:
        return None
    exists = db.query(model.id).filter(model.id == rid, model.company_id == company_id).first()
    if not exists:
        raise ValueError(f"{model.__name__} {rid} does not belong to company {company_id}")
    return rid


def _find_queued_or_running_provisioning_job(db: Session, company_id: UUID, idempotency_key: str):
    from database_utils.models.isp import ProvisioningJob, ProvisioningJobStatus
    # Duplicate enqueue (e.g. a retriggered workflow) is a no-op success.
    # Pre-check instead of catching the unique violation: a mid-workflow
    # rollback would discard this run's execution audit rows. A genuine
    # race still trips uq_provisioning_job_company_idem and fails the step.
    # Cycle 7 (doc 25 §6.3): PENDING_INFORM joined the in-flight set in nc1a's
    # uq_provisioning_job_company_idem predicate — this pre-check must match
    # it, or a re-enqueue while a job is parked trips the unique index and
    # fails the step instead of deduping.
    return db.query(ProvisioningJob).filter(
        ProvisioningJob.company_id == company_id,
        ProvisioningJob.idempotency_key == idempotency_key,
        ProvisioningJob.status.in_([
            ProvisioningJobStatus.QUEUED,
            ProvisioningJobStatus.RUNNING,
            ProvisioningJobStatus.PENDING_INFORM,
        ]),
    ).first()


def _execute_enqueue_provisioning(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """
    Insert a durable provisioning_job row (ADR-005). The workflow engine never
    talks to devices — the provisioning worker claims and executes the job.

    Two mutually exclusive modes:

    Mode A — service-path resolution (Cycle 10, doc 35, `use_service_path`):
    {
      "use_service_path": true,
      "purpose": "ACTIVATION",              # default ACTIVATION; free-form E1
                                             # custom purposes allowed
      "client_service_id": "{{trigger.after.linked_object_id}}",  # REQUIRED
      "variables": {...},                   # optional; overrides resolved vars
      "idempotency_key": "activate-{{trigger.after.linked_object_id}}",
      "max_attempts": 3
    }
    database_utils.utils.provisioning_resolution.resolve_provisioning walks the
    service's network path and picks a playbook per device — CPE_NOT_SET /
    CPE_NOT_ATTACHED / PLAYBOOK_NOT_BOUND / PLAYBOOK_INACTIVE all FAIL the step
    visibly (founder hard requirement: resolution failures are never silent).
    A task/event with no resolvable CLIENT_SERVICE target is NOT an error — it
    is a non-matching event (see the skip rule below).

    Mode A opens a ProvisioningRun, not a single job: a path spans several
    devices and therefore several playbooks. The run queues its first child;
    the worker advances the rest.

    Mode B — explicit playbook (pre-Cycle-3 shape, unchanged):
    {
      "playbook_id": "uuid",
      "variables": {"onu_serial": "{{trigger.after.serial_number}}"},
      "client_service_id": "{{trigger.resource_id}}",
      "inventory_item_id": null,
      "integration_id": null,
      "idempotency_key": "install-{{trigger.resource_id}}",
      "max_attempts": 3
    }

    NOTE: `config` here is ALREADY template-resolved — execute_step calls
    _resolve_template(step.action_config, context) before dispatching to any
    action handler, so use_service_path/purpose/client_service_id/
    idempotency_key arrive pre-resolved. Mode B keeps its own
    `_resolve_template` subset-resolution below for its pre-Cycle-3 shape
    (idempotent re-resolution of an already-resolved string is a no-op) but
    it is not required for mode A.
    """
    if "use_topology" in config:
        # A tenant workflow that survived ng2 untouched. Failing the step
        # visibly is the point: silently treating it as mode B would enqueue
        # nothing and look like a healthy no-op forever.
        raise ValueError(
            "ENQUEUE_PROVISIONING: 'use_topology' was retired with the topology "
            "chain (doc 35). Use 'use_service_path': true — the playbooks are "
            "resolved per device by walking the service's network path."
        )

    use_service_path = bool(config.get("use_service_path"))
    if use_service_path and config.get("playbook_id"):
        raise ValueError(
            "ENQUEUE_PROVISIONING: 'use_service_path' and 'playbook_id' are "
            "mutually exclusive"
        )

    if use_service_path:
        return _execute_enqueue_provisioning_path(db, config, company_id)
    return _execute_enqueue_provisioning_explicit(db, config, context, company_id)


def _execute_enqueue_provisioning_explicit(
    db: Session,
    config: dict,
    context: dict,
    company_id: UUID,
) -> dict:
    """Mode B: explicit playbook_id (pre-Cycle-3 shape, unchanged behavior)."""
    from database_utils.models.isp import Playbook, ProvisioningJob, ProvisioningTrigger
    from database_utils.models.isp import ClientService, InventoryItem
    from database_utils.models.crm import Integration
    # Was missing since doc 33 (fabaed9) — the namespacing change added the
    # input_key() call here but only imported it in the other mode, so every
    # explicit-playbook enqueue has NameError'd since.
    from database_utils.utils.provisioning_resolution import input_key

    playbook_id = config.get("playbook_id")
    if not playbook_id:
        raise ValueError(
            "ENQUEUE_PROVISIONING step requires 'playbook_id' in action_config "
            "(or 'use_service_path': true)"
        )

    playbook = db.query(Playbook).filter(
        Playbook.id == playbook_id,
        Playbook.company_id == company_id,
        Playbook.is_active == True,
    ).first()
    if not playbook:
        raise ValueError(f"Active playbook {playbook_id} not found for company {company_id}")

    resolved = _resolve_template(
        {
            "variables": config.get("variables") or {},
            "client_service_id": config.get("client_service_id"),
            "inventory_item_id": config.get("inventory_item_id"),
            "idempotency_key": config.get("idempotency_key"),
        },
        context,
    )

    idempotency_key = resolved.get("idempotency_key") or None
    if idempotency_key:
        existing = _find_queued_or_running_provisioning_job(db, company_id, idempotency_key)
        if existing:
            return {"enqueued": False, "deduped": True,
                    "job_id": str(existing.id), "idempotency_key": idempotency_key}

    job = ProvisioningJob(
        company_id=company_id,
        playbook_id=playbook.id,
        # doc 33: explicit-playbook mode runs no topology resolution, so every
        # variable here is author input and takes the `input.*` namespace.
        variables={input_key(str(k)): v for k, v in (resolved["variables"] or {}).items()},
        client_service_id=_owned_or_none(db, ClientService, resolved.get("client_service_id"), company_id),
        inventory_item_id=_owned_or_none(db, InventoryItem, resolved.get("inventory_item_id"), company_id),
        integration_id=_owned_or_none(db, Integration, config.get("integration_id"), company_id),
        idempotency_key=idempotency_key,
        max_attempts=int(config.get("max_attempts", 3)),
        triggered_by=ProvisioningTrigger.WORKFLOW,
    )
    db.add(job)
    db.flush()

    return {"enqueued": True, "job_id": str(job.id), "playbook_id": str(playbook.id)}


def _execute_enqueue_provisioning_path(
    db: Session,
    config: dict,
    company_id: UUID,
) -> dict:
    """Mode A: service-path resolution (doc 35 §5). `config` is already
    template-resolved by execute_step (see the docstring above) — no second
    _resolve_template pass is needed here.

    Ordering (doc 20a workflow-provisioning verifier fix on step ordering):
    skip rule -> idempotency dedupe -> resolve -> open run. The idempotency key
    never depends on resolution output, so a re-fire while a run is already in
    flight must dedupe even if the graph drifted in between — resolving first
    would turn a harmless dedupe into a spurious FAILED execution.
    """
    from database_utils.models.isp import ClientService, ProvisioningTrigger
    from database_utils.utils.provisioning_resolution import (
        ResolutionError, input_key,
    )
    from database_utils.utils.provisioning_runs import create_run, find_in_flight_run

    rid = _uuid_or_none(config.get("client_service_id"))
    if rid is None:
        # D-E2-1: the trigger is "any task moved into column X" -- tasks with
        # no CLIENT_SERVICE linkage are non-matching events, not resolution
        # errors. A visible skip (not FAILED) avoids flooding operator-visible
        # failures for ordinary drags on a shared done-column.
        return {"enqueued": False, "skipped": True, "reason": "NO_CLIENT_SERVICE_TARGET"}

    purpose = str(config.get("purpose") or "ACTIVATION").strip().upper()

    idempotency_key = config.get("idempotency_key") or None
    if idempotency_key:
        existing = find_in_flight_run(db, company_id, idempotency_key)
        if existing:
            return {"enqueued": False, "deduped": True,
                    "run_id": str(existing.id), "idempotency_key": idempotency_key}

    svc = (
        db.query(ClientService)
        .options(
            joinedload(ClientService.cpe_item),
            joinedload(ClientService.service_plan),
        )
        .filter(ClientService.id == rid, ClientService.company_id == company_id)
        .first()
    )
    if svc is None:
        # A CLIENT-linked task's UUID won't exist in client_service (ownership
        # + type guard); a forged cross-tenant id -- also not found -- so
        # also a skip, never a data leak.
        return {"enqueued": False, "skipped": True, "reason": "NO_CLIENT_SERVICE_TARGET"}

    # doc 33: automation-authored variables are author input, so they land in
    # the `input.*` namespace. Merged flat they could SHADOW a resolved system
    # variable (a step named `variables: {serial: ...}` silently overriding the
    # resolver's device serial); namespacing makes that impossible rather than
    # merely discouraged.
    extra = {
        input_key(str(key)): value
        for key, value in (config.get("variables") or {}).items()
    }

    try:
        run = create_run(
            db, svc, purpose=purpose,
            triggered_by=ProvisioningTrigger.WORKFLOW,
            idempotency_key=idempotency_key,
            extra_variables=extra,
        )
    except ResolutionError as e:
        raise ValueError(
            f"Provisioning resolution failed for service {rid} (purpose={purpose}): "
            f"{e.code} — {e.detail}. Errors: {json.dumps(e.errors)}"
        )

    return {
        "enqueued": True,
        "run_id": str(run.id),
        "purpose": purpose,
        "devices": len(run.plan or []),
    }


def _resolve_template(value: Any, context: dict) -> Any:
    """
    Recursively resolve {{template}} variables in strings, lists, and dicts.

    Supported patterns:
      {{trigger.resource_id}}         — from context["trigger"]["resource_id"]
      {{trigger.after.FIELD}}         — from context["trigger"]["after"][FIELD]
      {{trigger.before.FIELD}}        — from context["trigger"]["before"][FIELD]
      {{steps.STEP_UUID.FIELD}}       — from context[STEP_UUID][FIELD]
      {{now}}                         — current Guatemala-tz timestamp (ISO 8601)
    """
    import re
    if isinstance(value, str):
        def replacer(match: re.Match) -> str:
            expr = match.group(1).strip()
            parts = expr.split(".")
            try:
                if expr == "now":
                    return now_gt().isoformat()
                if parts[0] == "trigger":
                    trigger = context.get("trigger", {})
                    if len(parts) == 2:
                        return str(trigger.get(parts[1], ""))
                    elif len(parts) >= 3 and parts[1] in ("after", "before"):
                        nested = trigger.get(parts[1]) or {}
                        return str(nested.get(parts[2], ""))
                elif parts[0] == "steps" and len(parts) >= 3:
                    step_result = context.get(parts[1], {}) or {}
                    return str(step_result.get(parts[2], ""))
            except Exception:
                pass
            return match.group(0)  # leave unresolved templates as-is
        return re.sub(r"\{\{(.+?)\}\}", replacer, value)
    elif isinstance(value, dict):
        return {k: _resolve_template(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_template(item, context) for item in value]
    return value


def _execute_http_request(
    db: Session,
    step: WorkflowStep,
    context: dict,
    company_id: UUID,
) -> dict:
    """
    Make an HTTP request to an external API using a registered Integration.

    action_config format:
    {
      "integration_id": "uuid",
      "method": "POST",
      "path": "/configure-router",
      "headers": {"Content-Type": "application/json"},  # optional extra headers
      "body": {"client_id": "{{trigger.resource_id}}"}  # template-enabled JSON body
    }

    Uses httpx.Client (sync) — execute_step is a regular def.
    Lazy-imports both httpx and Integration to avoid hard dependencies in models-utils.
    """
    import httpx  # lazy: httpx is in backend-erp, not models-utils
    from database_utils.models.crm import Integration

    config = step.action_config
    integration_id = config.get("integration_id")
    if not integration_id:
        raise ValueError("HTTP_REQUEST step requires 'integration_id' in action_config")

    integration = db.query(Integration).filter(
        Integration.id == integration_id,
        Integration.company_id == company_id,
    ).first()
    if not integration:
        raise ValueError(f"Integration {integration_id} not found for company {company_id}")

    # Build URL
    path = config.get("path", "")
    url = integration.base_url.rstrip("/") + path

    # SEC-6: block SSRF to internal / cloud-metadata / private addresses BEFORE
    # attaching stored credentials or making any request.
    from database_utils.utils.ssrf import validate_url_no_ssrf, SSRFValidationError
    try:
        validate_url_no_ssrf(url)
    except SSRFValidationError as e:
        raise ValueError(f"HTTP_REQUEST step blocked (SSRF guard): {e}")

    # Build headers: start with configured extra headers, then inject auth
    headers: dict = dict(config.get("headers", {}))
    creds: dict = integration.credentials or {}

    auth_type = integration.auth_type.value
    if auth_type == "API_KEY":
        header_name = creds.get("header_name", "X-API-Key")
        headers[header_name] = creds.get("api_key", "")
    elif auth_type == "BEARER_TOKEN":
        headers["Authorization"] = f"Bearer {creds.get('token', '')}"
    elif auth_type == "BASIC_AUTH":
        import base64
        raw = f"{creds.get('username', '')}:{creds.get('password', '')}"
        encoded = base64.b64encode(raw.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    # NONE: no auth headers added

    # Resolve template variables in the body
    body = _resolve_template(config.get("body"), context)

    method = config.get("method", "POST").upper()

    try:
        with httpx.Client(timeout=30) as client:
            response = client.request(method, url, headers=headers, json=body)
    except httpx.TimeoutException:
        raise ValueError(f"HTTP_REQUEST step timed out after 30s: {method} {url}")
    except httpx.RequestError as e:
        raise ValueError(f"HTTP_REQUEST step network error: {e}")

    return {
        "status_code": response.status_code,
        "response_body": response.text[:10_000],
        "success": response.is_success,
        "url": url,
        "method": method,
    }


def detect_cycle(steps_count: int, edges: List[tuple]) -> bool:
    """
    Detect cycles using Kahn's algorithm (topological sort).
    Returns True if a cycle is detected.
    """
    if steps_count == 0:
        return False

    adj = defaultdict(list)
    in_deg = [0] * steps_count
    for u, v in edges:
        adj[u].append(v)
        in_deg[v] += 1

    queue = deque([i for i in range(steps_count) if in_deg[i] == 0])
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in adj[node]:
            in_deg[neighbor] -= 1
            if in_deg[neighbor] == 0:
                queue.append(neighbor)

    return visited != steps_count
