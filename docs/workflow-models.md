# Workflow Models

## Description

SQLAlchemy ORM models for the business automation engine
(`database_utils/models/workflow.py`): templates, workflows, triggers, the step
DAG, and execution history. Engine semantics are documented separately in
[workflow-engine.md](workflow-engine.md).

## Goal

Store workflow definitions and execution state for the automation engine that
ships **in this library** (`database_utils/utils/workflow_engine.py`) and is
invoked by backend-erp after entity mutations.

## Models (in `database_utils/models/workflow.py`; table names in parens)

| Model | Purpose |
|-------|---------|
| `WorkflowTemplate` (workflow_template) | **Globally seeded blueprint** (from `alembic/seeds/isp_seed.py`) that tenants install as concrete workflows |
| `Workflow` (workflow) | Tenant automation definition |
| `WorkflowTrigger` (workflow_trigger) | Event trigger — `TriggerEventType` CREATED/UPDATED/DELETED + `field_conditions` JSON |
| `WorkflowStep` (workflow_step) | Action node — `StepActionType`: `UPDATE_FIELD`, `CREATE_ENTITY`, `HTTP_REQUEST`, `ENQUEUE_PROVISIONING`, `CREATE_ORDER`, `CREATE_TASK` (the last two added by the **irreversible** `c1e_install_actions` ALTER TYPE migration) + `action_config` JSON |
| `WorkflowStepEdge` (workflow_step_edge) | DAG edge between steps |
| `WorkflowExecution` (workflow_execution) | Execution instance (`ExecutionStatus`) |
| `WorkflowStepExecution` (workflow_step_execution) | Per-step result/output |

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
  `workflow_template.definition` rows
- **Seeds**: `alembic/seeds/isp_seed.py` seeds purpose-based workflow-template
  blueprints. The `new-installation` blueprint is at **v4** (ships with the
  no-op `tk1_new_installation_v4` revision): its installation-fee param is type
  `service_plan` (key `installation_fee_plan_id`) and the `CREATE_ORDER` item
  uses `service_plan_id` — v3's required `product` param pointed at the retired
  legacy Product catalog and blocked fresh tenants; installed v3 copies keep
  running (`product_id` deprecated-but-honored during the rollback window)
- **Workflow schemas** (`schemas/workflow.py`, `schemas/workflow_template.py`)

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
