# Workflow Engine & Provisioning Resolution

## Description

The platform's core automation logic lives in this library:

- `database_utils/utils/workflow_engine.py` (the largest file in the repo):
  trigger matching and asynchronous DAG execution of workflow steps
- `database_utils/utils/provisioning_resolution.py`: resolution of a service's
  **network path** into one playbook per device plus the variable frames
- `database_utils/utils/network_graph.py` and
  `database_utils/utils/provisioning_runs.py`: the traversal the resolver walks,
  and the multi-device run it produces (Cycle 10, doc 35). Full reference in
  [utilities.md](utilities.md)

Data model reference: [workflow-models.md](workflow-models.md) and
[isp-models.md](isp-models.md).

## Why it lives here (import-direction constraint)

backend-erp imports models-utils, **never the reverse** (documented in the
`provisioning_resolution.py` module docstring). Anything the engine needs must
therefore live in this repo — which is why `provisioning_resolution.py` was
moved *down* from backend-erp in Cycle 3, so the engine's
`ENQUEUE_PROVISIONING` step can call it directly.

## Engine flow (`workflow_engine.py`)

1. **Trigger** — backend-erp CRUD routers fire the engine after entity
   mutations via `asyncio.create_task`. `check_workflow_triggers` matches the
   resource type + event (`CREATED`/`UPDATED`/`DELETED`) against active
   `WorkflowTrigger` rows, including their `field_conditions` JSON.
2. **Execution** — `execute_workflow` walks the step DAG
   (`WorkflowStep` + `WorkflowStepEdge`), calling `execute_step` per node and
   recording `WorkflowExecution` / `WorkflowStepExecution` rows.
3. **Step actions** (`StepActionType`):
   - `UPDATE_FIELD` — mutate a field on the trigger entity
   - `CREATE_ENTITY` — create a related entity
   - `HTTP_REQUEST` — call an external `Integration` target; URLs are guarded
     by `utils/ssrf.py` `validate_url_no_ssrf` (SEC-6)
   - `ENQUEUE_PROVISIONING` — two mutually exclusive modes, see
     [below](#enqueue_provisioning-two-modes)
   - `CREATE_ORDER` — creates an order with **service-plan resolution** and a
     **billing denylist** (Cycle 2/3 additions preventing automation from
     touching billing-critical fields)
   - `CREATE_TASK` — creates a task board item
   (`CREATE_ORDER` and `CREATE_TASK` were added by the irreversible
   `c1e_install_actions` `ALTER TYPE` migration.)
4. **Trigger-context variables** — `utils/workflow_fields.py` handles
   variable/field substitution from the triggering entity's context.

Catalog-merge compatibility: bridge-less legacy products are treated as
`SERVICE` inside the engine (see [limitations.md](limitations.md)).

## `ENQUEUE_PROVISIONING`: two modes

`config` arrives **already template-resolved** — `execute_step` calls
`_resolve_template(step.action_config, context)` before dispatching to any action
handler, so `use_service_path` / `purpose` / `client_service_id` /
`idempotency_key` are pre-resolved by the time the handler sees them.

### `use_topology` is retired and fails loudly

A workflow still carrying the old key raises immediately:

> `ENQUEUE_PROVISIONING: 'use_topology' was retired with the topology chain
> (doc 35). Use 'use_service_path': true — the playbooks are resolved per device
> by walking the service's network path.`

That is deliberate. `ng2_topology_drop` rewrites the key in every installed
`workflow_step.action_config` and `workflow_template.definition`, so a surviving
`use_topology` means a workflow the migration never saw. Silently treating it as
mode B would enqueue nothing and look like a healthy no-op forever.

### Mode A — service-path resolution (`use_service_path: true`)

```json
{
  "use_service_path": true,
  "purpose": "ACTIVATION",
  "client_service_id": "{{trigger.after.linked_object_id}}",
  "variables": {},
  "idempotency_key": "activate-{{trigger.after.linked_object_id}}",
  "max_attempts": 3
}
```

`_execute_enqueue_provisioning_path` opens a **`ProvisioningRun`, not a single
job** — a path spans several devices and therefore several playbooks. It queues
the run's first child; the worker advances the rest via
`provisioning_runs.advance_run`. The step result is
`{"enqueued": true, "run_id": ..., "purpose": ..., "devices": len(run.plan)}`.

Order of operations is **skip rule → idempotency dedupe → resolve → open run**.
The idempotency key never depends on resolution output, so a re-fire while a run
is already in flight must dedupe even if the graph drifted in between; resolving
first would turn a harmless dedupe into a spurious FAILED execution. The dedupe
uses `find_in_flight_run`, whose in-flight set (QUEUED / RUNNING /
`PENDING_INFORM`) mirrors `uq_provisioning_run_company_idem`.

`CPE_NOT_SET` / `CPE_NOT_ATTACHED` / `PLAYBOOK_NOT_BOUND` / `PLAYBOOK_INACTIVE`
all **fail the step visibly** (founder hard requirement: resolution failures are
never silent). A trigger with no resolvable CLIENT_SERVICE target is *not* an
error — it is a non-matching event and returns
`{"skipped": true, "reason": "NO_CLIENT_SERVICE_TARGET"}`, which is also what a
forged cross-tenant id gets, so a skip never leaks tenancy.

Automation-authored `variables` are namespaced through `input_key()` before being
merged, so a step declaring `variables: {serial: ...}` cannot shadow the
resolver's `device.serial`.

### Mode B — explicit playbook (pre-Cycle-3 shape, unchanged)

`playbook_id` + `variables` + optional target ids. It enqueues one standalone
`ProvisioningJob` with `run_id` NULL, runs no path resolution, and namespaces
every author variable under `input.*`. Its idempotency pre-check
(`_find_queued_or_running_provisioning_job`) treats QUEUED, RUNNING **and
`PENDING_INFORM`** as in-flight (Cycle 7 fix, doc 25 §6.3) — matching nc1a's
partial-unique-index predicate, so a re-enqueue while a job is parked dedupes
instead of tripping the index. `use_service_path` and `playbook_id` together are
rejected as mutually exclusive.

## Provisioning resolution (`provisioning_resolution.py`)

Rewritten in Cycle 10 (doc 35 §3.2). It starts from
`client_service.cpe_item_id`, walks the company graph to the root
(`network_graph.resolve_path`, leaf → root), skips passive nodes, and picks a
playbook per remaining node via **node override → device-type default → none**.
It returns a `ResolvedProvisioning` (`path`, `steps`, `shared_variables`,
`device_variables`), never a single playbook.

The full contract — error codes, dataclass fields, the `device.*` / `cpe.*` /
`path.<category>.*` variable namespace, and the fail-open regex warning — lives
in [utilities.md](utilities.md).

Callers: the workflow engine (`ENQUEUE_PROVISIONING` mode A) and backend-erp
(provisioning worker + manual provision endpoint).

## Templates

`WorkflowTemplate` rows are globally seeded blueprints
(`alembic/seeds/isp_seed.py`, purpose-based blueprint versions) that tenants install as
concrete workflows — this powers the founder "install automation" flow.

## Tests

`tests/` is 28 files / **252 tests**, all on in-memory SQLite
(`pytest.ini`: `asyncio_mode = auto`; CI needs only placeholder `POSTGRES_*`).

`tests/conftest.py` holds the shared `db` and `plant` fixtures. `plant` builds a
**real** in-memory SQLite network graph — `CORE-1 (router) → OLT-1 (olt) →
SPL-1 (splitter) → SPL-2 (splitter) → ONT-1 (onu)`, both splitters passive, with
ACTIVATION and SUSPENSION bound to the router/olt/onu device types — rather than
a set of fakes: resolution now runs recursive CTEs and two binding lookups, so a
hand-rolled fake DB would assert the shape of the mock instead of the behaviour
of the query.

Graph-cycle coverage: `test_network_graph_model.py` (the columns, self-FK
RESTRICT, CHECKs, relationships), `test_network_graph_traversal.py` (ordering,
cycle detection, the depth cap, and cross-tenant isolation proven by writing an
illegal edge with raw SQL), `test_playbook_binding.py` (per-type/per-node
uniqueness, precedence, `is_passive`), `test_provisioning_resolution.py`,
`test_provisioning_run.py`, `test_network_graph_migrations.py` (static assertions
about the ng1/ng2 revision files) and `test_isp_seed_passive.py` (passive
classification converges once and never reverts an admin edit).
`test_topology_purpose.py` and `test_playbook_topology.py` were **deleted** with
the models they covered.

Also covered: the engine, the ENQUEUE_PROVISIONING PENDING_INFORM dedupe
(`test_workflow_dedupe.py`), the service lifecycle machine, model↔migration
constant parity (`test_core_config_constants.py`), ba1/cf1 migration guardrails,
SSRF, JWT, tokens and the email surface.

The behavioural proof that the ng1/ng2 chain applies cleanly against a real
production dump and is byte-identical on a second run is the **rehearsal**, not a
unit test.
