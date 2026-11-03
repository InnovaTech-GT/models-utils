# Workflow Models

## Description

SQLAlchemy ORM models for the business automation engine
(`database_utils/models/workflow.py`): workflows, triggers, the step DAG, and
execution history (the `WorkflowTemplate` catalog was dropped by `ld1_legacy_drop`). Engine semantics are documented separately in
[workflow-engine.md](workflow-engine.md).

## Goal

Store workflow definitions and execution state for the automation engine that
ships **in this library** (`database_utils/utils/workflow_engine.py`) and is
invoked by backend-erp after entity mutations.

## Models (in `database_utils/models/workflow.py`; table names in parens)

| Model | Purpose |
|-------|---------|
| `Workflow` (workflow) | Tenant automation definition |
| `WorkflowTrigger` (workflow_trigger) | Event trigger — `TriggerEventType` CREATED/UPDATED/DELETED + `field_conditions` JSON |
| `WorkflowStep` (workflow_step) | Action node — `StepActionType`: `UPDATE_FIELD`, `CREATE_ENTITY`, `HTTP_REQUEST`, `ENQUEUE_PROVISIONING`, `CREATE_ORDER`, `CREATE_TASK` (the last two added by the **irreversible** `c1e_install_actions` ALTER TYPE migration) + `action_config` JSON |
| `WorkflowStepEdge` (workflow_step_edge) | DAG edge between steps |
| `WorkflowExecution` (workflow_execution) | Execution instance (`ExecutionStatus`; `WAITING`/`CANCELLED` since wf1) |
| `WorkflowStepExecution` (workflow_step_execution) | Per-step result/output |
| `WorkflowVersion` (workflow_version) | v2: one version of a workflow's JSON definition (`WorkflowVersionStatus` DRAFT/PUBLISHED/SUPERSEDED) |
| `AutomationTemplate` (automation_template) | v2: global template catalog, one immutable row per (key, version), seeded by backend-erp |
| `WorkflowEvent` (workflow_event) | v2: business-event outbox drained by the WorkflowWorker |

## Engine v2 (revision `wf1_workflow_engine_v2`, 6.23.0, doc 63-workflow-engine)

Both engines share the `workflow` row (one list, one bot picker, one export),
split by `workflow.engine_version`:

- **1 = legacy.** Defined by `workflow_trigger`/`workflow_step`/`workflow_step_edge`
  rows; run in-process by `utils/workflow_engine.py`, whose
  `find_matching_workflows` filters `engine_version == 1`. Removed by the C1 cleanup.
- **2 = v2.** No trigger/step/edge rows. The definition is
  `workflow_version.definition` (schema 1, see `schemas/workflow_v2.py`:
  `{schema, trigger:{type, key, config}, start, nodes:[{key, action, name?, config}],
  edges:[{from, to, on: completed|always, branch: yes|no?}], params, customer_reply}`).
  `workflow.active_version_id` points at the PUBLISHED version runs start from
  (FK `use_alter`, ORM `post_update` so install can flush workflow + v1 +
  pointer in one go). At most one DRAFT per workflow (`uq_workflow_version_draft`).
  `trigger_type`/`trigger_key` are denormalized for the consumer's match
  (`ix_workflow_version_trigger`). Template bookkeeping on `workflow`:
  `source_template_key`, `source_template_version` (of the ACTIVE definition),
  `template_params`, `ignored_template_version`.

v2 runs extend the legacy execution tables:

- `workflow_execution`: `company_id` (backfilled), `version_id` (history only),
  `definition` — the **frozen copy** the executor runs (NULL = legacy run),
  `dedupe_key` (unique per workflow when set: `ev:`/`task_done:`/`date:`/`bot:`/`test:`),
  `depth`, `is_test`, `test_options`, `context` `{trigger, steps}`, `cursor`
  `{queue, recoveries}`, `resume_at`, `claim_token` + `heartbeat_at` (lease),
  `client_id`/`client_service_id` (subject, not FKs), `customer_message` (bot
  reply), `error_code`, `updated_at`. `ix_workflow_execution_runnable` is the
  claim scan.
- `workflow_step_execution`: `node_key` (unique per run: idempotency),
  `action_key`, `attempts`, `simulated`, `input` (resolved config),
  `error_code`, `resume_at`; `ix_step_execution_messages` backs the daily
  message cap (`company.workflow_daily_message_cap`, NULL = 500).

**Event outbox** (`workflow_event`, a copy of `provisioning_event`): core code
calls `utils/workflow_events.emit_business_event(db, company_id, name, payload)`
in the transaction that made the change (flushes, never commits; unknown names
raise). `depth`/`source_run_id` come from the `current_depth`/`current_run_id`
ContextVars the executor sets, for the loop guard. `EVENT_NAMES`:
`payment.confirmed`, `client_service.activated/suspended/reactivated/cancelled`,
`client_service.plan_changed` (internal), `task.completed`, `task.created`,
`provisioning_run.closed`. The four `client_service.*` lifecycle events are
emitted by `ClientService`'s `status` validator itself (every ORM writer), only
for an attached instance whose status really changes (PENDING_INSTALL→ACTIVE
activated, SUSPENDED→ACTIVE reactivated, →SUSPENDED, →CANCELLED).

`ServiceBoost` (`service_boost`, in `models/isp.py`) is the D13 speed boost;
see [isp-models.md](isp-models.md).

## Connections to Other Components

- **`database_utils/utils/workflow_engine.py`** (in THIS repo — not
  backend-erp): reads these models to match triggers and execute the DAG;
  backend-erp routers fire it after CRUD mutations via `asyncio.create_task`
- **CRM models** ([crm-models.md](crm-models.md)): triggers fire on CRM entity
  events; `CREATE_ORDER`/`CREATE_TASK` create CRM rows; `HTTP_REQUEST` steps
  use `Integration` credentials (URL-guarded by `utils/ssrf.py`)
- **ISP models** ([isp-models.md](isp-models.md)): `ENQUEUE_PROVISIONING` either
  creates one standalone `ProvisioningJob` (explicit `playbook_id` mode) or, with
  **`use_service_path: true`**, opens a `ProvisioningRun` whose children are one
  job per configured device — resolved by `utils/provisioning_resolution.py`
  walking the service's network path. The retired `use_topology` key raises;
  `ng2_topology_drop` rewrites it in installed `workflow_step.action_config` and
  rows (the `workflow_template` table itself was dropped by `ld1_legacy_drop`)
- **Seeds**: the workflow-template blueprint catalog was removed by
  `ld1_legacy_drop`; `isp_seed.py` no longer seeds workflows. Installed copies
  are independent rows and keep running
- **Workflow schemas** (`schemas/workflow.py`; `schemas/workflow_template.py` deleted)

## Key Implementation Details

- `field_conditions` JSON: per-field value conditions for trigger matching
- `action_config` JSON: step-specific configuration per action type;
  trigger-context variables are handled by `utils/workflow_fields.py`
- Steps form a DAG connected by edges; execution follows the graph
- `ExecutionStatus` lifecycle recorded per execution and per step
- Execution history is retained (a step-execution snapshot was added by the
  `c2e_step_exec_snapshot` migration)

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
