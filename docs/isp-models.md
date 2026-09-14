# ISP Models

## Description

SQLAlchemy ORM models for the ISP vertical (`database_utils/models/isp.py` —
the largest model module): service catalog, subscriber services, the network
plant, and provisioning. Built across Cycles 2–3 of the Uplink ISP pivot
(platform ADRs 002/003/005/006); Cycle 7 (core config, doc 25, revision
`nc2a_core_config`) added the CORE/EDGE tier axis, the device management surface
and the subscriber install state machine — detailed in
[network-models.md](network-models.md). Cycle 8 (network UX, doc 26, revision
`c8a_playbook_topology`) dropped the old
`Playbook.target_vendor`/`target_category_id` targeting columns. The
service-lifecycle cycle added the **service status machine**
(`PURPOSE_TO_STATUS`, `ALLOWED_TRANSITIONS`, `purpose_allowed_for_status`) that
backs the per-purpose lifecycle actions.

**Cycle 10 (the company network graph, doc 35, revisions `ng1_network_graph` +
`ng2_topology_drop`) is the largest change to this module since the Cycle 2
entity merge.** `Topology`, `TopologyDeviceType` and `TopologyPlaybook` are
**deleted**. The plant is now one company-wide tree of `InventoryItem` rows, a
service names one CPE, playbooks bind to device types (overridable per node), and
a provisioning run spans several devices. The structural detail lives in
[network-models.md](network-models.md); this page records what changed *here*.

## Models (in `database_utils/models/isp.py`; table names in parens)

### Catalog

| Model | Purpose |
|---|---|
| `ServicePlan` (service_plan) | Merged catalog item. `ServicePlanType`; `CatalogKind` is the **hybrid kind** introduced when the legacy CRM `Product` was absorbed by the Cycle 2 catalog merge (revision `c2a_catalog_merge`). `provisioning_params` (JSON) holds the plan's playbook parameters as `[{key, value, description, scope}]` — `scope` is `plan` (one shared value) or `service` (declared here, valued on each `ClientService`); a row without `scope` is plan-scoped, so pre-feature rows keep their behaviour |

### Subscriber services

| Model | Purpose |
|---|---|
| `ClientService` (client_service) | A client's subscription to a plan; `ClientServiceStatus`. Absorbed recurring-order billing in `c2b_service_billing`; the dual-write link to legacy `recurring_order` is retained during the rollback window. Cycle 7 (`nc2a`): `install_state` (`INSTALL_STATES` CHECK: NOT_INSTALLED/IN_PROGRESS/INSTALLED, separate from billing `status`, written only by backend-erp's `recompute_install_state`) + `installed_at` (stamped on first INSTALLED, never cleared). Brownfield adoption (doc 30, `ba1_attested_adoption`): `adopted_at` (TIMESTAMPTZ NULL) + `adopted_by_user_id` (UUID FK → `user`, SET NULL, `adopted_by` relationship) + `adoption_note` (VARCHAR NULL) + partial index `ix_client_service_adopted` (`company_id` WHERE `adopted_at IS NOT NULL`) — a persistent attestation FACT substituting for a missing SUCCEEDED activation job inside backend-erp's install_state derivation (never a state, never writes `install_state`, the CHECK is unchanged); writable only via the adopt/un-adopt endpoints behind the ADMIN-only `client_services.adopt` permission; historical `installed_at` may be pre-set at adopt time only when still NULL. `ACTIVATION_EVIDENCE_*` module constants back the backend-computed `activation_evidence` field. Per-service provisioning parameters (`sp1_service_params`): `provisioning_params` (JSON NULL, `[{key, value}]`) holds this service's VALUES for the parameters its plan declares with `scope='service'` — the plan owns the declaration, the service owns only the value, and both resolve to `{{service_plan.<key>}}`. Distinct from `connection_params`, which stays free-form and is not a playbook variable source. **Cycle 10 (`ng1`/`ng2`)**: `topology_id` is **dropped**; `cpe_item_id` (FK → `inventory_item`, SET NULL, indexed, `cpe_item` relationship) and `path_changed_at` (TIMESTAMPTZ NULL, stamped on a re-parent above the CPE, cleared by a SUCCEEDED non-dry-run ACTIVATION run) replace it |
| `ServiceSuspension` (service_suspension) | Suspension records with `SuspensionReason` |

#### Service lifecycle status machine (doc 35)

Module-level constants in `models/isp.py`, next to the purpose constants. They
live **here, not in backend-erp**, because the workflow engine resolves the same
purposes and the import direction is strictly downward (see CLAUDE.md). They are
the single source of truth for both the backend's pre-flight gate and the
frontend's per-purpose action buttons (a button is enabled only when the machine
allows the transition **and** the service's path resolves a playbook for that
purpose — since Cycle 10 that means the CPE is set and attached, and every
non-passive node on the path has a binding for the purpose).

- `PURPOSE_TO_STATUS` — the status each canonical purpose writes:
  `SUSPENSION → SUSPENDED`, `REACTIVATION → ACTIVE`, `DEPROVISION → CANCELLED`,
  and **`ACTIVATION → None`**. `None` is deliberate: activating a service
  *enqueues only*. The `PENDING_INSTALL → ACTIVE` flip is written by backend-erp's
  `recompute_install_state` once the install state reaches `INSTALLED` — i.e.
  after the provisioning job succeeds. Encoding `None` means a caller writing
  `new_status = PURPOSE_TO_STATUS[purpose]` cannot short-circuit that and mark a
  service ACTIVE before the network agrees.
- `ALLOWED_TRANSITIONS` — legal status writes. `PENDING_INSTALL → {ACTIVE,
  CANCELLED}`, `ACTIVE → {SUSPENDED, CANCELLED}`, `SUSPENDED → {ACTIVE,
  CANCELLED}`, and `CANCELLED → {}`: **CANCELLED is terminal**, a cancelled
  service is never revived (re-selling creates a NEW `client_service` row).
  DELETE additionally refuses any non-cancelled service, so cancel is the only
  way out.
- `purpose_allowed_for_status(purpose, current_status) -> bool` — accepts a
  `ClientServiceStatus` or its string value and a purpose in any case/spacing the
  normalizer would take. Four cases:
  1. **ACTIVATION** is special-cased (it writes no status, so it has no target to
     look up): legal **only from `PENDING_INSTALL`**.
  2. **REACTIVATION** is *also* special-cased: it is the exact inverse of
     SUSPENSION, so it is legal **only from `SUSPENDED`**. The generic lookup
     would wrongly allow it from `PENDING_INSTALL`, because its target `ACTIVE`
     happens to be a legal edge out of `PENDING_INSTALL` — but that edge belongs
     to ACTIVATION and is owned by `recompute_install_state`, not by a status
     write. Taking the generic path there would mark a **never-installed service
     ACTIVE and start billing it** (status/`activation_date`/`billing_status`/
     `next_generation_date` all written) for an install that never happened and a
     CPE that was never linked. The legacy `POST /client-services/{id}/reactivate`
     endpoint has always rejected this; both paths share `_apply_reactivation`,
     so they must agree.
  3. Any other canonical purpose — legal iff its target status is in
     `ALLOWED_TRANSITIONS[current_status]`.
  4. A **tenant-defined custom purpose** (purposes are extensible free strings,
     see `PLAYBOOK_PURPOSE_PATTERN`) falls through to `True`: it is enqueue-only
     and writes no status, so there is no transition to police. It must never
     raise — an unknown purpose is normal tenant config, not a bug.

  A `None` purpose or an unparseable status returns `False`.

Covered by `tests/test_service_lifecycle.py`.

### Inventory (ADR-002 hybrid model)

| Model | Purpose |
|---|---|
| `DeviceCategory` (device_category) | **Global SaaS-admin table** (Cycle 3 `c3b` converted it from an enum to a FK table). Cycle 7 (`nc2a`): `tier` (`DEVICE_CATEGORY_TIERS` CHECK: CORE/EDGE; NULL = unclassified) — admin-editable, backfilled by key. Cycle 10 (`ng1`): `is_passive` (bool, NOT NULL, default false) — signal-passive gear that is **on** the path and never configured. `tier` says where a device sits; `is_passive` says whether it is ever configured |
| `DeviceType` (device_type) | Tenant device type catalog. Cycle 7: `cli_platform` (netmiko platform id, free string; NULL → generic drivers) |
| `Warehouse` (warehouse) | Stock location |
| `InventoryItem` (inventory_item) | Physical unit — **and, since Cycle 10, a node of the company network graph**: `parent_id` (self-FK, ON DELETE RESTRICT, indexed) + `network_attached` (bool) + the `parent`/`children` relationships. Root = attached with no parent; warehouse stock is simply not attached. Cycle 7 mgmt surface: `mgmt_host`/`mgmt_port`/`cli_protocol` (`CLI_PROTOCOLS` CHECK: ssh/telnet) + worker-stamped `mgmt_last_check_at`/`mgmt_last_check_ok`. NAT transport (2026-08-13, `nat1`): `nat_port` (Integer, range-CHECKed, unique per company where set — the external gateway port that `dst-nat`s to this device, never conflated with `mgmt_port`) + `mgmt_host_key` (pinned SSH host key, TOFU) — see [network-models.md](network-models.md) |
| `EquipmentEvent` (equipment_event) | Equipment lifecycle events |

### The network graph (ADR-003 → Cycle 10 tree)

There is **no topology model and no separate node model**. A network element is
an `InventoryItem` that has been attached to the company's tree via
`parent_id` + `network_attached`; roles come from `device_category`
(`tier`, `is_passive`). Structure, constraints and the two guard triggers:
[network-models.md](network-models.md).

This is the third attempt at a plant model and the history matters when reading
the code. Doc 07 specified exactly this tree; it shipped as
`network_node`/`network_node_type`/`network_link` and was **deleted** by
`c2d_graph_removal` because it was decorative — provisioning never traversed it,
it was a free-form graph with a link overlay, and it carried a second type
catalog alongside `device_type`. `Topology` replaced it as a named, ordered chain
of device *types*, and that in turn is what Cycle 10 removed. What is different
this time: the tree is **load-bearing** (it is the only source of the config
path), it is a **strict tree** with a single parent and no link overlay, and
nodes *are* `InventoryItem`s so there is **one catalog** and one identity.

### Provisioning (ADR-005/006)

| Model | Purpose |
|---|---|
| `Playbook` (playbook) | Declarative provisioning steps. A plain company-scoped library row with **no ownership column**: Cycle 8 dropped `target_vendor`/`target_category_id` (+ the `target_category_ref` relationship and `target_category` @property) and Cycle 10 dropped the `topology_id` it briefly gained. One playbook can therefore serve several device types |
| `DeviceTypePlaybook` (device_type_playbook) | Cycle 10 — the type-level default playbook for a purpose. UNIQUE (device_type_id, purpose) |
| `InventoryItemPlaybook` (inventory_item_playbook) | Cycle 10 — one node's override of its device type's default. UNIQUE (inventory_item_id, purpose). Precedence: **node override → device-type default → none** |
| `ProvisioningRun` (provisioning_run) | Cycle 10 — one service-path run: several devices, several playbooks. Snapshots the resolved `path`, the ordered `plan` and the variable `frames` at creation; children are created lazily, one at a time, leaf → root |
| `ProvisioningJob` (provisioning_job) | Durable job queue row — `ProvisioningJobStatus`, `ProvisioningTrigger`; consumed by backend-erp's `provision-worker` process (SKIP LOCKED claiming). Cycle 10 adds `run_id`/`run_position`; **`run_id` NULL means a standalone job** (explicit-playbook, ACS reboot, connectivity probe) and nothing about those changed |

## Connections to Other Components

- **backend-erp**: primary consumer (ISP routers + the provisioning worker)
- **Workflow engine** ([workflow-engine.md](workflow-engine.md)): the
  `ENQUEUE_PROVISIONING` step's **`use_service_path`** mode walks the service's
  network path via `utils/provisioning_resolution.py` and opens a
  `ProvisioningRun`. The retired `use_topology` key raises
- **Seeds**: `alembic/seeds/isp_seed.py` seeds ISP permissions, tier modules,
  purpose-based workflow-template blueprints, and the device_category baseline
  (Cycle 7: baseline entries carry a CORE/EDGE tier; Cycle 10: they also carry
  `is_passive`. Both classifications use the same gate — backfill only while no
  row anywhere is classified — so super-admin edits survive every re-seed)
- **CRM models** ([crm-models.md](crm-models.md)): `ClientService` links to
  `Client` and dual-writes legacy `RecurringOrder` rows

## Key Implementation Details

- All models: UUID v4 primary keys + `created_at`/`updated_at`
- `DeviceCategory` is global (SaaS-admin managed), unlike the tenant-scoped
  inventory tables; seeds converge with admin edits (idempotent upserts)
- Cycle-7 value sets are CHECK-constrained strings, never PG enums:
  `DEVICE_CATEGORY_TIERS`, `CLI_PROTOCOLS`, `INSTALL_STATES` (constants in
  `models/isp.py`, SQL fragments byte-identical with the nc2a migration)
- **The graph is the provisioning source of truth.** A service supplies exactly
  two network inputs — which CPE (`client_service.cpe_item_id`) and which node it
  hangs off (that item's `parent_id`) — and the whole configuration path is
  derived by walking to the root, leaf → root

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
