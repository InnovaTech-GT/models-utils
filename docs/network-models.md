# Network Configuration Models

## Description
SQLAlchemy models for Uplink's network configuration layer. Cycle 5 Phase 1
(TR-069 / GenieACS CPE management) added revisions **nc1a** (five tables +
`ProvisioningJob` extensions + `PENDING_INFORM` status) and **nc1b** (the append-only
trigger on `device_action_log`). Cycle 7 Phase 2 (core-device CLI config, doc 25)
added revision **nc2a_core_config** — no new tables, only columns on existing ones
(see the Cycle 7 section below). Cycle 10 (**the company network graph**, doc 35,
revisions `ng1_network_graph` + `ng2_topology_drop`) replaced the per-service
topology chain with one company-wide tree of inventory items — this is the
largest change on this page and has its own section below. All live in
`database_utils/models/isp.py`.

## Goal
Persist per-tenant device secrets, transport config, the serial→tenant mapping that
makes a shared multi-tenant-blind GenieACS safe, the provisioning enable gate, and an
immutable device audit trail — plus the durable-job columns for resumable, per-device,
idempotent execution, and (Cycle 10) **the physical plant itself**, so a
subscriber's configuration path is derived by traversal instead of declared as a
pre-baked chain.

## New Models (in `database_utils/models/isp.py`)

| Model | Table | Key Fields | Purpose |
|-------|-------|-----------|---------|
| `DeviceCredential` | `device_credential` | name, kind (CHECK: CREDENTIAL_KINDS), username, `secret_ciphertext`/`dek_wrapped`/`kek_id`, fingerprint, binding FKs (inventory_item/device_type) | Envelope-encrypted per-tenant device secret (canon C1/C19). Secret never round-trips — Out schema exposes only `has_secret` + fingerprint. `tr1` dropped the third binding FK, `network_access_id`, with the table it pointed at: resolution is now inventory_item > device_type > **both NULL = the company default** |
| `AcsDeviceRegistration` | `acs_device_registration` | serial_number, oui, company_id (**nullable** = QUARANTINED), genieacs_device_id, first/last_inform_at, cwmp_cr_* connection-request creds, `created_by_user_id` (FK user SET NULL, fg1 — author of a single/bulk pre-registration; NULL for bootstrap/quarantine rows) + `created_by` relationship | Serial/OUI→tenant mapping — the tenant-stamping keystone (canon C13); global `(oui, serial)` unique so two tenants can't claim one CPE — plus the partial UNIQUE `uq_acs_registration_serial_no_oui` on `(serial_number) WHERE oui IS NULL` (`ac1`), because the two-column UNIQUE does NOT cover NULL-oui rows |
| `ProvisioningSettings` | `provisioning_settings` | company_id (unique), enabled (default **false**), default_inform_interval, **`dial_target`** (`device`\|`gateway`, NOT NULL default `device`), **`proxy_kind`** (`none`\|`socks5`, NOT NULL default `none`), **`proxy_address`**, **`gateway_host`**, **`acs_base_url`** (read-only), **`acs_auth_required`** (Boolean NOT NULL default false — the Capa 3 gate), **`cwmp_credential_id`** / **`cwmp_pending_credential_id`** (FK device_credential SET NULL) — all eight from `tr1_transport_axis`; **`ztp_enabled`** (Boolean NOT NULL default false, `zt1`, doc 43: the tecnicos INSTALL closeout starts the ACTIVATION run) | Per-tenant provisioning gate **and** transport/ACS configuration — a per-tenant singleton (canon C6 + C9). Absence of a row = provisioning DISABLED (fail-safe). `tr1_transport_axis` folded the whole multi-row `network_access` table in here — see the transport-axis section below |
| `DeviceActionLog` | `device_action_log` | actor_kind, actor_user_id, device_kind, device_identity, action, before_data/after_data (secret-redacted JSON), provisioning_job_id | Append-only device audit trail (canon C14). No `updated_at`; immutability enforced by a Postgres `BEFORE UPDATE OR DELETE` trigger (nc1b) |

## `ProvisioningJob` extensions (nc1a)

- `status` gains **`PENDING_INFORM`** (`ProvisioningJobStatus`): the job parks when a
  TR-069 connection-request task returns 202; the worker slot is released and a poller
  settles it once the inform arrives. Added via `ALTER TYPE … ADD VALUE` in an
  autocommit block.
- `dry_run` (bool, default false) — canon C7; a SUCCEEDED dry-run stamps
  `playbook.last_dry_run_version`.
- `pending_step_index` (int) — the parked step to settle on inform.
- `pending_task_ids` (JSON) — GenieACS NBI task ids being polled.
- `heartbeat_at` (datetime) — the lease reaper re-queues stale RUNNING jobs.
- `device_lock_key` (str) — per-device serialization key. Since the
  provisioning-concurrency fix it is written **only by the worker's claim**
  (never for dry runs) and cleared on every terminal transition; producers
  insert it NULL.
- `claim_token` (UUID, revision `pc1_provisioning_claim_token`) — fence token,
  set per claim, NULL when not executing; every worker write after the claim is
  conditional on `(id, status, claim_token)`.
- **Indexes**: the idempotency partial-unique index now includes PENDING_INFORM in the
  in-flight set; a new `uq_provisioning_job_device_lock` partial-unique index enforces
  at most one live job per `device_lock_key` (canon C11).

## Other additive changes (nc1a)

- `playbook.last_dry_run_version` (int) — gates live jobs behind a matching dry-run.
- `device_type.provisioning_enabled` (bool, default true) — per-device-type opt-out gate.
- `inventory_item.oui` (str) — matched against informing CPEs by `acs_sync`.
- `client_service.provisioning_state` (JSON) — learned network identifiers written at
  job settlement, read back by suspension/reactivation/deprovision playbooks.
- **17 new permissions** for the network-config endpoints.

## Cycle 7 (nc2a_core_config) — core-config additions (doc 25 §2)

Phase 2 targets CORE-tier devices (OLTs, routers, switches) over generic
netmiko CLI drivers. All additive columns on existing tables:

| Table | New columns | Purpose |
|---|---|---|
| `device_category` | `tier` (CHECK: `DEVICE_CATEGORY_TIERS` CORE\|EDGE, nullable) | CORE = shared infrastructure (one device serves many subscribers — after Cycle 10, everything above a CPE in the graph); EDGE = per-subscriber CPE; NULL = passives/unclassified. SaaS-admin editable (key stays immutable). nc2a backfills CORE ← ROUTER/SWITCH/OLT, EDGE ← ONU/CPE_ROUTER/ACCESS_POINT by key. Cycle 10 adds the orthogonal `is_passive` flag (below) — `tier` says *where* a device sits, `is_passive` says *whether it is ever configured* |
| `device_type` | `cli_platform` (free string, deliberately no CHECK) | netmiko platform id (`huawei_smartax`, `cisco_ios`, ...); NULL → drivers fall back to `generic` / `generic_telnet` |
| `inventory_item` | `mgmt_host`, `mgmt_port`, `cli_protocol` (CHECK: `CLI_PROTOCOLS` ssh\|telnet), `mgmt_last_check_at`, `mgmt_last_check_ok` | Management surface: how CLI drivers reach a CORE device. `mgmt_port` NULL → driver default (22/23). The `mgmt_last_check_*` stamps are worker-owned (written when a `core_connectivity_check` job reaches terminal state), read-only in the API |
| ~~`topology_device_type`~~ | ~~`inventory_item_id`~~ | **Gone.** nc2a pinned the concrete shared device serving a chain position; Cycle 10 dropped the whole `topology_device_type` table (`ng2_topology_drop`) because sharing is now structural — a node above the CPE is shared by everything beneath it, and a node with `client_service_id` set is dedicated. Nothing needs pinning |
| `client_service` | `install_state` (NOT NULL default `NOT_INSTALLED`, CHECK: `INSTALL_STATES`), `installed_at`; index `ix_client_service_company_install_state` | Subscriber install state machine (NOT_INSTALLED / IN_PROGRESS / INSTALLED), deliberately **separate** from billing `status`. Written exclusively by backend-erp's `recompute_install_state` (not on Update schemas); `installed_at` stamps the FIRST transition to INSTALLED and is never cleared |

Also in Cycle 7 (same revision cycle, no DDL):
- `PLAYBOOK_DRIVERS` (schemas/playbook.py) gains **`ping`** — backend-erp's
  connectivity-probe driver, used by the per-company `core_connectivity_check`
  system playbooks (doc 25 §4.3/§5.1).
- `PlaybookStep.target_item_id` (+ same field on `PlaybookPrecondition`) — step
  targeting for the CLI/ping drivers: an inventory_item id or a `{{variable}}`
  the executor renders. Cycle 10 makes this the *only* targeting surface: a
  playbook binds to one device type and therefore runs on exactly one device, so
  the executor defaults the target to `{{device.item_id}}` and `target_item_id`
  is the power-user override (`target_position` is gone — see below).
- Workflow-engine dedupe fix: the `ENQUEUE_PROVISIONING` in-flight pre-check now
  includes `PENDING_INFORM` (matching nc1a's idempotency-index predicate) — see
  [workflow-engine.md](workflow-engine.md).
- `isp_seed.py`: `DEVICE_CATEGORIES` entries carry the tier (ONU display name →
  'ONU / ONT' for fresh inserts); a gated backfill classifies pre-nc2a rows only
  while NO row has a tier yet, so super-admin tier edits (including clear-to-NULL)
  survive every re-seed.

## Cycle 8 (c8a_playbook_topology) — topology-owned playbooks — **superseded**

Cycle 8 made playbooks **topology-owned**: it dropped `playbook.target_vendor`
and `playbook.target_category_id` (+ FK `fk_playbook_target_category_id`, the
`target_category_ref` relationship and the `target_category` @property) and added
a nullable `playbook.topology_id` FK CASCADE.

The vendor/category drop **stands** — those columns are gone for good. The
`topology_id` half was **reversed by Cycle 10**: `ng2_topology_drop` drops the
column along with the `topology` table it pointed at, and ownership moves into
the two binding tables below. A `playbook` row is once again a plain
company-scoped library entry with no ownership column of its own, which is what
lets one playbook serve several device types (one "MikroTik core config" for two
router models) — impossible while ownership was a column.

`c8a` also added `PlaybookStep.target_position`; that too is gone (doc 35 §4.4).

## Cycle 10 (ng1_network_graph + ng2_topology_drop) — the company network graph (doc 35)

The per-service **topology chain** is replaced by **one company-wide network
graph**. `Topology`, `TopologyDeviceType` and `TopologyPlaybook` are deleted
models; `schemas/topology.py` is deleted.

Why the chain was the wrong model: a carrier has one physical plant, not N
chains, so the same OLT and core router were re-declared (and re-pinned) in every
topology; adding a subscriber meant picking a pre-baked chain instead of stating
a physical fact; playbooks belonged to the chain rather than to the equipment;
and positional variables (`chain[3]`, `target_position`) broke the moment a path
length differed — which in a real plant it always does.

Two revisions, deliberately split so a reviewer can read "what appears" and "what
disappears" independently. The chain is
`lc2_retire_susp_react` → **`ng1_network_graph`** → **`ng2_topology_drop`** (the Cycle-10 head at the time).

### The tree lives on `inventory_item` (ng1, doc 35 §2.1)

There is no new node entity — a network element **is** an `InventoryItem` that
has been attached to the graph. Everything provisioning needs is already keyed by
`inventory_item.id` (`mgmt_host`/`mgmt_port`/`cli_protocol`, the device type and
its category, credential bindings, `nat_port`, the ACS registration,
`client_service_id`, `warehouse_id`, `uq_provisioning_job_device_lock`), so a
parallel node table would either duplicate all of it or force a join at every one
of those call sites — and would re-create exactly the `network_node_type` /
`device_type` duality that `c2d_graph_removal` deleted.

| Column | Definition | Notes |
|---|---|---|
| `inventory_item.parent_id` | UUID NULL, self-FK `fk_inventory_item_parent` → `inventory_item.id` **ON DELETE RESTRICT**, indexed `ix_inventory_item_parent_id` | RESTRICT is deliberate: deleting an OLT must not silently promote the 400 subscribers behind it to roots. Re-parent or detach the children first |
| `inventory_item.network_attached` | BOOLEAN NOT NULL DEFAULT false | Whether the item is part of the plant at all. **Root** = attached with no parent (the core router / headend); warehouse stock, RMA and a spare ONT in a van are simply not attached. Two flags rather than one because `parent_id IS NULL` alone cannot tell "this is the core router" from "this ONT is still in the van" |

Constraints and indexes:

| Name | Rule |
|---|---|
| `ck_inventory_item_parent_attached` | `parent_id IS NULL OR network_attached` — you cannot hang off a parent while unattached |
| `ck_inventory_item_not_self_parent` | `parent_id IS NULL OR parent_id <> id` |
| `ix_inventory_item_company_attached` | partial index on `(company_id)` WHERE `network_attached` — root/tree listing |

Model-side, `InventoryItem` gains the `parent` (with `remote_side=[id]`) and
`children` relationships.

### Link ports (`lp1_link_ports`)

Two free-text labels describe the `parent_id` edge itself:

| Column | Definition | Notes |
|---|---|---|
| `inventory_item.parent_port` | VARCHAR(64) NULL | Port **on the parent** this item plugs into ("PON 16", "OUT 3", "sfp-sfpplus1") |
| `inventory_item.uplink_port` | VARCHAR(64) NULL | This item's **own** port facing the parent ("GE1"); usually blank for splitters |

`uq_inventory_item_parent_port` — partial unique index on `(parent_id,
parent_port)` WHERE `parent_port IS NOT NULL`: a parent port feeds one child.
**Unmapped since `pt2_unmap_port_labels` (doc 40 §4.2 C8a, 5.0.0).** Doc 40
supersedes them with real ports and links (below): the model no longer maps the
two columns or the index, backend-erp stopped reading and writing them
(`PATCH /network/nodes/{id}/link` is gone) and derives `NetworkNodeOut.parent_port`
/ `uplink_port` from the link only. `pt2` drops the index (a C8a backend no
longer clears a re-parented item's label, so the index would turn a move next
to a same-labelled sibling into a unique violation); the DB keeps the two
columns until `pt3_drop_port_labels` (C8b) drops them.

### Port-level topology (`pt1_port_topology`, doc 40 §3.1)

Additive and inert: the revision creates no rows. Ports and links only appear
once a backend that writes them (cycle C2) is deployed.

**Templates.** `device_type.port_template` (JSON, `none_as_null`, NULL = no
template) is a list of port groups, e.g.
`[{"name": "{slot}/{n}", "slots": [1], "start": 1, "count": 16, "medium": "PON", "direction": "DOWN"}]`.
`schemas/inventory.py` validates it (`PortTemplateGroup` + `validate_port_template`)
and `expand_port_template` turns it into one `PortSpec(slot, number, name,
medium, direction)` per port: only `{slot}`/`{n}` placeholders (`str.replace`,
never `str.format`), a group `name` pattern of at most 64 characters and at
most 256 `slots` entries (both bounded before expansion), slots 0–255, start 0–4095, count 1–256, ≤ 32 groups and
≤ 1,024 ports, names matching `PORT_NAME_PATTERN`
(`^[A-Za-z0-9][A-Za-z0-9/:._ -]{0,31}$` — they reach device CLIs), unique
case-insensitively, PON ports unique on (slot, number, direction).
`ck_device_type_ports_serialized` (`port_template IS NULL OR is_serialized`)
backs the schema's `PORT_TEMPLATE_REQUIRES_SERIALIZED`. `device_type.path_role`
(VARCHAR(32), `PATH_ROLE_PATTERN`, not secret-named, not unique) names the
node for `path.<role>.*`; `path_role_shadows_category(db, role)` is the DB half
of the backend's 422 `PATH_ROLE_SHADOWS_CATEGORY`.

**`InventoryItemPort`** (`inventory_item_port`):

| Column | Definition |
|---|---|
| `item_id`, `company_id` | composite FK `fk_item_port_item` → `inventory_item (id, company_id)` ON DELETE CASCADE |
| `name` | VARCHAR(32): "1/4", "9:1", "ether2", "OUT 6", "IN", "PON" |
| `slot` | SMALLINT NULL, 0–255 — structural only, an OLT slot is not a line card |
| `number` | SMALLINT, 0–4095 |
| `medium` / `direction` / `origin` | `PORT_MEDIA` (ETH, PON) / `PORT_DIRECTIONS` (UP, DOWN, ANY) / `PORT_ORIGINS` (TEMPLATE, ITEM = per-item addition) |

Indexes: `uq_item_port_name` on `(item_id, lower(name))`; `uq_item_port_pon_number`
on `(item_id, coalesce(slot, -1), number, direction) WHERE medium = 'PON'`
(partial on both Postgres and SQLite); `uq_item_port_identity (id, item_id,
company_id)` is the target of the link FKs. `inventory_item` gains
`uq_inventory_item_id_company (id, company_id)` as the item-side target.

**`NetworkLink`** (`network_link`) — one row per device whose upstream port is
known: `up_item_id`/`up_port_id` (NOT NULL), `down_item_id`/`down_port_id`
(port nullable), `source` (`NETWORK_LINK_SOURCES`: OFFICE, FIELD, IMPORT),
`task_id` (SET NULL), `created_by_id` (SET NULL).

| Constraint | Rule |
|---|---|
| `fk_link_up_port` | `(up_port_id, up_item_id, company_id)` → port identity, NO ACTION |
| `fk_link_down_item` | `(down_item_id, company_id)` → item, ON DELETE CASCADE |
| `fk_link_down_port` | `(down_port_id, down_item_id, company_id)` → port identity, NO ACTION (MATCH SIMPLE) |
| `uq_link_up_port`, `uq_link_down_port`, `uq_link_down_item` | a port feeds one link; a device has one upstream link — still a tree |
| `ck_link_not_self` | `up_item_id <> down_item_id` |

The composite FKs make cross-tenant links, and links naming another item's port,
impossible on Postgres. Deleting only a device's own linked port fails
(NO ACTION); deleting the whole leaf ONU passes because Postgres checks NO ACTION
after the statement's cascades. ANY ports cannot be held twice (once up, once
down) by a constraint — the backend's single writer checks both under a lock.

**`parent_id` stays derived.** Invariant: for every link,
`inventory_item[down_item_id].parent_id = up_item_id` (the reverse does not hold:
a parent with no link is an *unported edge*). One backend helper writes both;
two **deferred** constraint triggers (`trg_network_link_parent_sync` on link
insert/update, `trg_inventory_item_link_sync` on `UPDATE OF parent_id`) call
`network_link_assert_parent()` at COMMIT and raise
`NETWORK_LINK_PARENT_MISMATCH` otherwise. Like the ng1 guards they live only in
the revision, never in SQLAlchemy metadata. `downgrade()` refuses while any link
or ITEM port exists. ORM relationships are all view-only:
`InventoryItem.ports`/`.uplink`, `InventoryItemPort.item`,
`NetworkLink.up_port`/`.down_port`/`.down_item`. Postgres-only behaviour is
pinned by `tests/pg/test_port_topology_pg.py` (CI job `pg`). SQLite test schemas
have neither the triggers nor enforced FKs, so graph tests call
`network_graph.assert_links_consistent(db)` instead.

**What provisioning reads (doc 40 §3.3).** The resolver loads the links of the
path in one company-scoped query and gives each node the port the node below
it hangs off — `out_slot`/`out_port`/`out_port_name`, only if the link's
`up_item_id` is that node (a reparent between the two reads must not borrow a
port) and never on the CPE. A device type's `path_role` becomes a
`path.<role>.*` frame when exactly one node on the path holds it. Templates read
the CO0648 values as `path.mufa_principal.out_port` (6), `path.mufa_secundaria.out_port`
(4) and `device.out_slot`/`device.out_port` on the OLT (1, 4). See
[utilities.md](utilities.md#provisioning_resolutionpy) for the variables and the
resolution-time refusal codes.

### Both guard triggers (ng1 only, never in SQLAlchemy metadata)

A CHECK constraint cannot express reachability, so "a node may not become its own
ancestor" and "a parent must belong to the same company" are unstateable as
CHECKs. The service layer runs the same checks first so the operator gets a
readable 422; **the triggers are the guarantee**, and they are what makes
cross-tenant traversal impossible rather than merely unlikely.

They live only in the revision, never in model metadata, because every consuming
service's test suite builds its schema with SQLite `create_all`, which cannot
parse plpgsql (precedent: `ck_topology_playbook_purpose_format`, `nc1b`).

| Trigger | Fires | Rejects (exception prefix) |
|---|---|---|
| `trg_inventory_item_graph_guard` → `inventory_item_graph_guard()` | `BEFORE INSERT OR UPDATE OF parent_id ON inventory_item` | `NETWORK_GRAPH_SELF_PARENT` (`NEW.parent_id = NEW.id`) · `NETWORK_GRAPH_PARENT_NOT_FOUND` · `NETWORK_GRAPH_CROSS_TENANT` (parent in another `company_id`) · `NETWORK_GRAPH_PARENT_DETACHED` (parent not `network_attached`) · `NETWORK_GRAPH_CYCLE` (recursive CTE over the prospective parent's ancestry reaches `NEW.id`) · `NETWORK_GRAPH_TOO_DEEP` (resulting depth ≥ `MAX_PATH_DEPTH` = 32) |
| `trg_inventory_item_detach_guard` → `inventory_item_detach_guard()` | `BEFORE UPDATE OF network_attached ON inventory_item` | `NETWORK_GRAPH_HAS_CHILDREN` — detaching a node that still has children would strand them: their `parent_id` would point outside the graph, `resolve_path` would stop early, and every subscriber behind it would silently resolve a shorter path |

The cycle walk inside the trigger is itself depth-bounded at 32. That bound is
not decoration: an unbounded recursive CTE over a pre-existing cycle does not
error, it **hangs**, and this runs on the provisioning hot path. The constant is
kept in sync by hand with `database_utils/utils/network_graph.MAX_PATH_DEPTH`;
if they ever disagree the trigger wins and traversal starts raising
`PATH_TOO_DEEP` on paths the database happily accepted.

### `device_category.is_passive` (ng1, doc 35 §2.3)

`BOOLEAN NOT NULL DEFAULT false`, platform-global and SaaS-admin editable exactly
like `tier`. A passive node **is** on the configuration path — it is shown, it
matters for troubleshooting and impact analysis, and it is addressable as
`path.<category>.*` — and it contributes **no** automation steps.

An explicit flag rather than an inference from "no playbook bound", because the
absence of a playbook cannot distinguish *"expected, it is a splitter"* from
*"someone forgot to bind an ACTIVATION playbook to this OLT"*. The first renders
as a calm grey chip; the second is a hard resolution error. Seeded `true` for
`SPLITTER`, `SPLICE_CLOSURE`, `PATCH_PANEL`, `ANTENNA` — and deliberately **not**
for `UPS` or `RADIO`, which are configurable devices that merely happen to sit
off the signal path in some plants.

### The two playbook binding tables (ng1, doc 35 §2.4)

A playbook runs on exactly one device, so it binds to the **equipment**: an OLT
is configured the same way regardless of whose traffic crosses it.

| Table | Columns | Constraints |
|---|---|---|
| `device_type_playbook` — the type-level default | `id`, `created_at`, `updated_at`, `company_id` (FK company CASCADE, indexed `ix_device_type_playbook_company_id`), `device_type_id` (FK device_type **RESTRICT**), `purpose` VARCHAR(50), `playbook_id` (FK playbook **RESTRICT**, indexed `ix_device_type_playbook_playbook_id`) | UNIQUE `uq_device_type_playbook_purpose` (device_type_id, purpose) · CHECK `ck_device_type_playbook_purpose_format` (`purpose ~ '^[A-Z][A-Z0-9_]{0,49}$'`) |
| `inventory_item_playbook` — the per-node override | same shape, with `inventory_item_id` (FK inventory_item **CASCADE**) | UNIQUE `uq_item_playbook_purpose` (inventory_item_id, purpose) · CHECK `ck_item_playbook_purpose_format` |

**Resolution order, per node per purpose: node override → device-type default →
none** — one helper, `provisioning_resolution.resolve_playbook_for`, so the
resolver, the path preview and the node detail endpoint cannot drift apart.

Both purpose CHECKs are applied in the **migration only**, never in metadata:
SQLite's `create_all` cannot parse the PG regex operator `~`, and the test suite
builds its schema that way (`ck_topology_playbook_purpose_format` precedent).
`purpose` stays the free-but-validated uppercase string it has always been —
tenants may add their own — normalized by `schemas/playbook.normalize_purpose`
against `PLAYBOOK_PURPOSE_PATTERN`.

### `ClientService`: two network inputs, nothing else (ng1/ng2, doc 35 §2.5)

| Change | Definition |
|---|---|
| **ADD** `cpe_item_id` | UUID NULL, FK `fk_client_service_cpe_item` → `inventory_item.id` **ON DELETE SET NULL**, indexed `ix_client_service_cpe_item_id`. SET NULL rather than RESTRICT: an RMA'd ONT must not block deleting the inventory row, and a service without a CPE is a legible state — it simply cannot be provisioned, reported as `CPE_NOT_SET` |
| **ADD** `path_changed_at` | TIMESTAMPTZ NULL. Stamped when someone re-parented a node above this service's CPE, so the path it was provisioned against is no longer the path it sits on. Cleared by a SUCCEEDED **non-dry-run ACTIVATION** run (`provisioning_runs.advance_run`). It **never** triggers provisioning on its own — pushing config to live carrier gear as a side effect of an org-chart edit is the wrong blast radius; the operator confirms |
| **DROP** `topology_id` | with its index `ix_client_service_topology_id` and its relationship |
| **DROP** `service_plan.default_topology_id` | the chain pre-fill has nothing left to point at |
| **DROP** `playbook.topology_id` | ownership moved to the binding tables |

The two network inputs an operator supplies are now (1) **which CPE** →
`client_service.cpe_item_id`, and (2) **which node it hangs off** → the CPE
item's own `parent_id` + `network_attached`. Everything else is derived by
traversal. `ClientService` gains the `cpe_item` relationship;
`InventoryItem.client_service` and `ClientService.equipment` must now name their
`foreign_keys` explicitly, because two FK paths join the two tables.

### `ProvisioningRun` (ng1, doc 35 §5)

A run spans several playbooks, and a single `ProvisioningJob` cannot honestly
represent that: `playbook_id` is a single NOT NULL FK and
`uq_provisioning_job_device_lock` is keyed per device, so one job touching three
devices could only ever hold one of the three locks. So the run is the container
and **each configured device gets its own child job**.

| Column | Definition |
|---|---|
| `id`, `created_at`, `updated_at`, `finished_at` | standard, plus a terminal stamp |
| `purpose` | VARCHAR(50) NOT NULL |
| `dry_run` | BOOLEAN NOT NULL DEFAULT false |
| `status` | the **existing** `provisioningjobstatus` PG enum reused via `PGEnum(..., create_type=False)` (a plain `sa.Enum` would try to `CREATE TYPE` and fail with DuplicateObject). Derived from the children |
| `path` | JSON NOT NULL — the whole resolved path **including passive nodes**, snapshotted at creation, so the run detail view shows what the path *was* when it ran, not what it is now |
| `plan` | JSON NOT NULL — one entry per child to run. **Engine v2 (doc 42 §6.1)**: phase-major (every device's PRECONDITIONS, CONFIGURATION, VERIFICATION, in build order — core bottom-up, CPE last; SUSPENSION/DEPROVISION reversed): `[{item_id, playbook_id, playbook_version, category_key, device_label, phase, steps: [{name, label, skip?}], probe?, ran_steps?}]`; ROLLBACK entries are **appended** when the run rolls back (the forward plan is a stable prefix). Pre-v2 runs have entries without `phase`, leaf → root. `path` entries carry `label`, `path_role`, `out_slot`/`out_port`/`out_port_name` and `playbook_version` (doc 40) |
| `frames` | JSON NOT NULL — `{"shared": {...}, "device": {item_id: {...}}, "definitions": {playbook_id: normalized definition}, "rollback": {cause_code, cause_error, no_rollback}}`, resolved **once** at run creation (`rollback` only once the run rolls back). Later children are built from this rather than re-resolved, so neither a re-parent nor a playbook edit landing mid-run can change what the remaining steps — or the rollback — do. Not exposed by the API |
| `phase` | VARCHAR(16) NULL, CHECK `ck_provisioning_run_phase` (`pe1`) — the phase of the entry being executed; NULL = a legacy (pre-v2) run |
| `error_code` / `error` | VARCHAR(40) / TEXT NULL (`pe1`) — the outcome: `PRECONDITION_FAILED`, `CONFIGURATION_FAILED`, `VERIFICATION_FAILED`, `CANCELLED`, `STRANDED_RUN_EXPIRED`, `ROLLBACK_INCOMPLETE`, `REVERTED` and the human detail. Written only by `provisioning_runs` |
| `outputs` | JSON NULL (`pe1`) — `[{key, label, value, unit, audience, secret, sensitive, shareable, ok, item_id, position, category_key, ref?}]`, last writer per `(item_id, key)`; a secret output stores `value` NULL + `ref` |
| `secrets_ciphertext` / `secrets_dek_wrapped` / `secrets_kek_id` | BYTEA / BYTEA / VARCHAR NULL (`pe1`) — the run's generated secrets (`{key: value}` JSON), envelope-encrypted like `DeviceCredential` with AAD `company_id:run.id`; NULL when the plan declares none |
| `idempotency_key` | VARCHAR NULL |
| `triggered_by` / `triggered_by_user_id` | `provisioningtrigger` enum (also reused) + FK user SET NULL |
| `company_id` / `client_service_id` | FK company CASCADE / FK client_service CASCADE, both indexed |

Indexes: `ix_provisioning_run_company_id`, `ix_provisioning_run_client_service_id`,
`ix_provisioning_run_service` (`client_service_id`, `created_at`), and the partial
unique `uq_provisioning_run_company_idem` on (`company_id`, `idempotency_key`)
WHERE `idempotency_key IS NOT NULL AND status IN ('QUEUED','RUNNING','PENDING_INFORM')`
— mirroring `uq_provisioning_job_company_idem` so a re-fire while a run is still
in flight dedupes instead of opening a second one.

`ProvisioningJob` gains:

- `run_id` — FK `provisioning_run.id` **ON DELETE CASCADE**, nullable, indexed
  `ix_provisioning_job_run_id`. **NULL for every job that is not part of a
  service-path run** — explicit-playbook jobs, ACS reboot/factory-reset, core
  connectivity probes. Nothing about those changes.
- `run_position` — INTEGER NULL, the 0-based index into `ProvisioningRun.plan`.
- `phase` — VARCHAR(16) NULL, CHECK `ck_provisioning_job_phase` (`pe1`): the
  phase its plan entry executes; NULL for standalone and legacy jobs. There is
  **no new `ProvisioningJobStatus` value** for engine v2.

Children are created **lazily, one at a time**, so at most one child of a run is
QUEUED or RUNNING at once. That needed no new `ProvisioningJobStatus` value (a
"BLOCKED" state would have had to be understood by every status consumer across
three services). Children are inserted with `device_lock_key` NULL; the worker's
claim takes the device lock, skips a child whose device is held, and keeps runs
on one service in FIFO order (provisioning-concurrency fix). What this buys: the
per-device lock is finally correct (each child locks exactly the device it
configures, at claim time), cancel becomes per-device (backend-erp's cancel
releases the child's lock and stops its run via `advance_run` in one commit;
a child is never retried on its own, `RUN_CHILD_NOT_RETRYABLE`, because its
terminal status already stopped the run), and `PENDING_INFORM` applies to the
CPE child alone instead of stalling the whole path. The mechanics
live in `utils/provisioning_runs.py` — see [utilities.md](utilities.md).

### `ng2_topology_drop` — guard, rewrite, then drop

Three phases, in this order and no other.

1. **Guards, before anything is touched.** `_assert_no_retired_syntax` scans
   `playbook.definition::text` for `chain[`, `edge_devices[`, `core_devices[`,
   `RETIRED_ALIAS` and `target_position`, and **raises**, listing the offending
   playbook ids. `_assert_every_service_has_a_cpe` raises on any `client_service`
   with `topology_id IS NOT NULL AND cpe_item_id IS NULL`.
   *Why raise rather than repair:* doc 35 forbids a compatibility shim, and a
   playbook still written against `chain[n]` would not fail loudly at run time —
   the renderer's guard would catch the unrendered token, but only after the job
   had been queued, claimed and partially executed. Stopping the release is
   cheaper than discovering it on a customer's OLT. And **there is no
   chain → graph backfill**: a chain names device *types*, a graph names device
   *instances*; deriving one from the other would mean inventing parent edges —
   fabricating physical facts about someone's plant.
2. **Idempotent rewrite.** `workflow_step.action_config` and
   `workflow_template.definition` (table since dropped) get the `ENQUEUE_PROVISIONING` config key
   `"use_topology"` → `"use_service_path"`. Predicate-guarded
   (`WHERE ... LIKE '%use_topology%'`) on both sides, so a second run matches
   nothing and leaves every row byte-identical. Only the key changes; values are
   untouched.
3. **Drops**, columns before tables (a referencing FK would block
   `DROP TABLE topology`): `client_service.topology_id` (+ its index),
   `service_plan.default_topology_id`, `playbook.topology_id`, then
   `topology_playbook`, `topology_device_type`, `topology` in that order. Every
   step is existence-guarded, so a re-run is a no-op.

`downgrade()` **raises `NotImplementedError`**: a graph cannot be turned back
into a set of named chains — the chains carried per-topology playbook bindings
and pinned positions the graph does not encode. Restore from a backup taken
before the release.

Production reality (verified 2026-08-06): production is at `a1f2b3c4d5e6` with 38
tables and **no ISP schema at all**, so both guards are vacuous there — the tables
they inspect are created empty by earlier revisions in the same release chain.
They exist for the local/staging databases that carry Cycles 1–9 data.

### Seed convergence (`alembic/seeds/isp_seed.py`)

`DEVICE_CATEGORIES` tuples widen from 4 to **5**:
`(key, name, sort_order, tier, is_passive)`. The passive classification follows
the `tier` precedent **exactly**: it fires only while
`SELECT COUNT(*) FROM device_category WHERE is_passive` is zero — i.e. while
nothing anywhere is classified. A per-row "is_passive is false" UPDATE could not
tell "never classified" from "a super-admin deliberately marked a splitter
active" (a tenant with managed splitters that report optical power would do
precisely that), and reverting that decision on every migrate is the bug the
`tier` block was written to avoid. The classification is also column-existence
gated, so running seeds at a pre-`ng1` migration position logs a warning and
skips instead of erroring.

## The transport axis (`tr1_transport_axis`, 2026-09-26) — and the `network_access` table it replaced

`network_access` is **gone**: the table, the `NetworkAccess` model,
`schemas/network_access.py`, `Company.network_accesses` and
`device_credential.network_access_id`. It had three problems, and the third is
the one that forced the change:

1. Its `kind` discriminator conflated two unrelated things — `acs` was a settings
   bucket, `outbound` was a transport.
2. It was MULTI-ROW only to serve a per-CIDR longest-prefix resolver over
   `mgmt_subnets`. That resolver was never implemented, and the idea is now
   **abandoned, not deferred** — do not reintroduce `mgmt_subnets`, per-CIDR
   paths or any "which path serves this address" lookup. The transport is
   tenant-wide.
3. `mode` enumerated the **cross product** of two independent questions, so the
   fifth real operator scenario (ZeroTier with managed routes, i.e. dial the
   device through a hop) had no value available at all.

The replacement is two orthogonal columns on the tenant singleton
`provisioning_settings`:

| Column | Values | Meaning |
|---|---|---|
| `dial_target` | `device` \| `gateway` | whose address do we dial — the DEVICE's own `mgmt_host`, or the tenant gateway that `dst-nat`s to it |
| `proxy_kind` | `none` \| `socks5` | is there a hop in front of that address, and of what sort |
| `proxy_address` | `host:port`, NULL | the SOCKS5 listener; **required** when `proxy_kind='socks5'` (`ck_provisioning_settings_proxy_address`) |
| `gateway_host` | String, NULL | the gateway's address on the path WE dial; **required** when `dial_target='gateway'` (`ck_provisioning_settings_gateway_host`). Deliberately `String`, not `INET`: a ZeroTier value is RFC1918 and a public one may be a DDNS hostname, so no routability assertion is possible or wanted. The per-device external port stays `inventory_item.nat_port` |

Four combinations, five scenarios, no schema change needed for a sixth:

| `dial_target` | `proxy_kind` | scenario | old `mode` |
|---|---|---|---|
| `device` | `none` | the devices have public IPs | `direct` |
| `gateway` | `none` | NAT + port map to a public IP | `nat_public` |
| `gateway` | `socks5` | NAT + port map reached via ZeroTier | `nat_zt` |
| `device` | `socks5` | a hub with managed routes into the LAN — WireGuard, ZeroTier or any other | `vpn`, and the ZeroTier variant had **no** `mode` value |

Canon C10's edge agent becomes `proxy_kind='agent'`, not a new mode. Tailscale or
Nebula need nothing.

**`proxy_kind` is LOAD-BEARING and must not be "optimised away" into
`proxy_address IS NOT NULL`.** `dial_target='device'` with no proxy is the
legitimate public-IP case, so without an explicit stored intent the system cannot
distinguish "no hop needed" from "a hub is intended but its address is missing" —
and the second would silently dial an RFC1918 address from the Railway container.
That is exactly the canon R23 fail-closed guarantee the old mode values existed to
provide. A blank/NULL `proxy_address` under `socks5` is a HARD ERROR
(`PROXY_NOT_PROVISIONED`), never a fallthrough. See
[utilities.md](utilities.md) for the resolver.

The hub TECHNOLOGY is deliberately NOT recorded: a Railway-internal
ZeroTier/Pylon proxy and an external WireGuard-hub VPS are the same thing to the
resolver, which is why the old `PYLON_NOT_PROVISIONED` / `VPN_NOT_PROVISIONED`
split collapsed into one code. What *does* still differ is a security
prerequisite, and it is a prerequisite rather than a code change:

> **The firewall on an EXTERNAL `proxy_address`.** A Railway-internal value
> (`pylon-acme.railway.internal:1080`) is unreachable from outside. A VPS value is
> not, and `microsocks` ships with **no authentication** — the listener MUST be
> restricted to Railway's egress, or anyone who learns the address has a route
> into the tenant's LAN.

Also moved off `network_access` by the same revision:

| Column | Note |
|---|---|
| `acs_base_url` | Informational ONLY and **read-only**: nothing in code reads it, its job is telling an installer what to type into a CPE. Deliberately absent from `ProvisioningSettingsUpdate`, which makes read-only structural rather than guard-dependent (the router applies `Update` with a blanket `setattr` loop). An `http://` value would turn every CWMP POST into a bodyless GET at Railway's edge, and a CPE pointed at a dead URL has no remote fix |
| `acs_auth_required` | The Capa 3 gate, unchanged in meaning (see the next section). `ck_network_access_acs_auth_required` ("only meaningful on the `acs` row") is not recreated — there is one row |
| `cwmp_credential_id` / `cwmp_pending_credential_id` | The tenant TR-069 credential and, during a rotation window, its successor. Two explicit FKs to `device_credential` (ON DELETE SET NULL) replacing "newest vs second-newest `HTTP_BASIC` row bound to the tenant's `acs` row" — an inference that was fragile in both directions: a third row was undefined, and the pair rested on a `created_at DESC, id DESC` tie-break. One credential per tenant is now true by construction |

`ck_provisioning_settings_cwmp_pair`
(`cwmp_pending_credential_id IS NULL OR cwmp_credential_id <> cwmp_pending_credential_id`)
is created. The companion "no pending without a current" CHECK is **deliberately
NOT**: both FKs are `ON DELETE SET NULL`, so deleting the current credential
during a rotation window would violate it through a *referential action* and turn
an ordinary `DELETE /device-credentials/{id}` into an IntegrityError surfacing as
a raw 500 — and a company delete has the same shape, since `device_credential` and
`provisioning_settings` both CASCADE from `company` and Postgres does not order
the SET NULL against the CASCADE. That invariant is a 409 in backend-erp's router,
which also closes the pre-existing gap that a plain DELETE of an ACS Inform
credential was never rollout-gated.

### What the historical revisions still mean

`nat1_gateway_transport` / `nat2_gateway_host_check` / `nat3_pylon_socks5` /
`vpn1_vpn_socks5` / `na1_kind_outbound` are **immutable and still in the chain** —
a fresh database migrates through all of them on its way to `tr1`. What they
created on `network_access` no longer exists afterwards; what they created
elsewhere does:

- `inventory_item.nat_port` (Integer, nullable, CHECK 1–65535, unique per
  `company_id` where set): the external port on the tenant's gateway that
  `dst-nat`s to this device. **Never** conflated with `mgmt_port`, which stays the
  device's real service port. Still live, still the gateway scenarios' operand.
- `inventory_item.mgmt_host_key` (String, nullable): pinned SSH host key (TOFU —
  recorded on first successful connect). Any later mismatch is a hard,
  non-retryable failure, never an auto-add.
- `mgmt_port`'s range CHECK, the gap `nc2a` left and the xlsx importer could
  exploit by writing 0 or 70000.

The data move `tr1` performs, for the record: per company, the default
`kind='outbound'` row and the default `kind='acs'` row fold into one settings row
(`direct`→device+none, `vpn`→device+socks5 carrying `vpn_socks5`,
`nat_public`→gateway+none, `nat_zt`→gateway+socks5 carrying `pylon_socks5`;
`gateway_host` verbatim), INSERTing with **`enabled = false`** where the tenant had
no settings row — canon C6 says absence means DISABLED, so inserting `true` would
silently enable provisioning. `mode='tunnel'` aborts the revision by name (it has
no mapping on the axis and the API never allowed it), and the non-default rows that
die with the table are counted and logged. `downgrade()` reverses the values
exactly but is not a true inverse: `name` is synthesised (`'ACS'`/`'Outbound'`),
`mgmt_subnets` is gone for good, and only the two cwmp credentials are re-bound.

## Capa 3 — per-tenant CWMP Inform authentication (`ac1_acs_tenant_auth`, 2026-09-25)

A CPE identifies itself to GenieACS by serial number alone, which is printed on
the device label, so tenant attribution rested on public information. Capa 3
adds credential proof. Tenant attribution itself stays serial-derived (GenieACS
is not patched); the password only authenticates it.

> **Storage note (2026-09-26).** `ac1` put `acs_auth_required` and the credential
> binding on `network_access`. `tr1_transport_axis` moved both onto
> `provisioning_settings` — the switch keeps its name and semantics exactly, and the
> credential became two explicit FKs. Everything below about the MECHANISM (the
> `cwmp.auth` expression, fail-open, the secret-storage rule) is unchanged.

| Table | New column / index | Purpose |
|---|---|---|
| `provisioning_settings` (`network_access` until `tr1`) | `acs_auth_required` (Boolean NOT NULL, `server_default false`) | The per-tenant switch. Default-OFF as a DB constraint; OFF means **ALLOW**. `ac1`'s companion CHECK `ck_network_access_acs_auth_required` ("only meaningful on the `acs` row") is not recreated on the singleton — there is one row |
| `acs_device_registration` | partial UNIQUE `uq_acs_registration_serial_no_oui` on `(serial_number) WHERE oui IS NULL` | Multi-tenancy. `uq_acs_registration_identity` is a plain two-column UNIQUE and Postgres treats NULLs as distinct, while `oui` is nullable and `_normalize_oui` returns `None` unchanged for an omitted OUI — so `(NULL, serial)` could repeat and the inform-auth lookup's `.first()` could hand one tenant's CWMP password to another tenant's CPE. `_normalize_oui` coerces a blank OUI to `None` (it used to return `''`, which put a second, index-invisible key on the same physical device) so every no-OUI write lands under this index |
| `permission` | row `device_credentials.reveal`, no role grant | Reading back a stored plaintext secret. **ADMIN role ONLY** — granted solely by the convergent seed's global-ADMIN cross-join; MANAGER is withheld (the name is in BOTH `isp_seed.ADMIN_ONLY_PERMISSIONS` and `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS`) and no `ISP_ROLES` entry — NOC included — lists it |

The credential itself needs **no new columns on `device_credential`**: it is an
ordinary `DeviceCredential` row of `kind='HTTP_BASIC'`, with `username` NULL
because the CWMP username is the per-device serial. The accept-both rotation
window is a **second** such row — rotation is create-new -> roll out ->
delete-old, and `POST /{id}/rotate` (which overwrites in place) is simply not used
for this credential. Hence no `pending_*` columns.

Since `tr1_transport_axis` the PAIR is stated rather than inferred:
`provisioning_settings.cwmp_credential_id` is the current secret and
`cwmp_pending_credential_id` the one being rolled out. `ac1` identified them as
"newest and second-newest `HTTP_BASIC` row bound via `network_access_id` to the
tenant's `acs` row", which left a third row undefined and made the pair depend on
a `created_at DESC, id DESC` tie-break; the two FKs remove both problems and make
one credential per tenant true by construction. The `ponytail:` note that used to
sit here — "more than two bound rows is undefined, add a constraint if a tenant
trips it" — is resolved rather than deferred: there is no third slot to fill.

### Secret storage: which primitive, and why (the rule to follow)

**Can the system ever need the original value back?**

- **No -> bcrypt.** Human login passwords (`User.password`): Uplink does the
  comparison itself, so a one-way hash is both sufficient and correct.
- **Yes -> `encrypt_secret` envelope AES-256-GCM** (`database_utils/utils/crypto.py`).
  Machine credentials: SSH, SNMP, WireGuard, TR-069 — including this CWMP Inform
  password. Something outside Uplink performs the comparison, so Uplink must be
  able to reproduce the plaintext.

That is one rule with two branches, not an inconsistency, and it is why the
`device_credentials.reveal` endpoint can exist at all — a deliberate,
permission-gated, audited exception to the write-only-secrets canon in
backend-erp's `routers/device_credentials.py`.

**Why the bcrypt variant was dropped.** An earlier branch
(`feat/network-config/acs-tenant-credentials`, revision `nc1d`) stored a bcrypt
hash of the tenant password, to standardize on the same utilities used for
`User.password`. It cannot work: the comparison does not happen in Uplink, it
happens inside GenieACS via `AUTH(username, password)`, which needs the expected
**plaintext** — Basic does `authentication["password"] === e[3]` and Digest feeds
the plaintext into the digest computation. There is no hand-GenieACS-a-hash
hook, and patching GenieACS is explicitly out of scope (attribution stays
serial-derived). bcrypt is also mutually exclusive with the reveal endpoint by
construction. `nc1d` was never merged. Luis Sactic's column shape informed the
final design.

**The gate fails OPEN, by design.** An EXT fault or timeout in the `cwmp.auth`
expression yields null, and the expression's mandatory `ELSE true` makes that
ALLOW. So a backend outage silently disables Capa 3 rather than bricking the
fleet — the opposite tradeoff from failing closed, and the deliberate one: the
alternative drops every CPE of every tenant during a blip. Deleting the
`cwmp.auth` Mongo document is the emergency brake (live within 5s, no deploy).

## Open value sets (CHECK-constrained strings, not PG enums)

Following the c3a/c3b precedent, driver-bounded value sets are CHECK-constrained
strings so adding a value is a plain transactional `ALTER` of the CHECK, never the
`ALTER TYPE … ADD VALUE` autocommit dance:

- `CREDENTIAL_KINDS`: SSH, TELNET, SNMP_COMMUNITY, TR069_CONNECTION_REQUEST, HTTP_BASIC,
  HTTP_BEARER, WIREGUARD, AGENT, **CLI_ENABLE** (`pe1`, doc 42 §9.3: the enable /
  privileged-mode password of a CLI device, bound like any kind — item > type >
  company default, `username` ignored; required by a playbook with
  `session.enable`, e.g. the CSR AN5516). `pe1` re-creates
  `ck_device_credential_kind` from its own copy of `_CREDENTIAL_KIND_CHECK`,
  pinned byte-identical by `tests/test_pe1_playbook_phases.py`
- `PROVISIONING_PHASES`: PRECONDITIONS, CONFIGURATION, VERIFICATION, ROLLBACK
  (`ck_provisioning_run_phase` / `ck_provisioning_job_phase`, NULL allowed) ·
  `TEARDOWN_PURPOSES`: SUSPENSION, DEPROVISION (`pe1`, doc 42)
- `DIAL_TARGETS`: device, gateway · `PROXY_KINDS`: none, socks5 — the transport axis
  (`tr1_transport_axis`, 2026-09-26; matching `ck_provisioning_settings_dial_target` /
  `_proxy_kind`, pinned by `tests/test_transport_axis.py`). They REPLACE
  `NETWORK_ACCESS_KINDS` / `NETWORK_ACCESS_MODES` / `NAT_MODES`, which are deleted
  along with the `network_access` table — see the transport-axis section above
- **Cycle 7**: `DEVICE_CATEGORY_TIERS`: CORE, EDGE · `CLI_PROTOCOLS`: ssh, telnet ·
  `INSTALL_STATES`: NOT_INSTALLED, IN_PROGRESS, INSTALLED (SQL CHECK fragments kept
  byte-identical between `models/isp.py` and the nc2a migration, guarded by
  `tests/test_core_config_constants.py`)

## Connections to Other Components
- **backend-erp** — the `genieacs` driver, worker loops (`acs_sync`, PENDING_INFORM
  poller, lease reaper), blast-radius gates, and the network-config routers consume all
  of these. Credentials are decrypted via `database_utils/utils/crypto.py`.
- **auth-erp `Company`** — back-populates `device_credentials`,
  `acs_device_registrations`, `provisioning_settings`, `device_action_logs` (all
  `ondelete=CASCADE`). `network_accesses` is gone with `tr1_transport_axis`.
- **acs-erp / GenieACS** — `acs_device_registration` maps informing CPEs to tenants.
- **Cycle 10** — backend-erp's `routers/network_graph.py` (tree, search, attach /
  reparent / detach, impact) and the binding endpoints read these tables through
  `utils/network_graph.py` and `utils/provisioning_resolution.resolve_playbook_for`;
  the provisioning worker advances runs through `utils/provisioning_runs.advance_run`.

## Key Implementation Details
- All tables: UUID PK + `created_at` (+ `updated_at` except the append-only
  `device_action_log`).
- `AcsDeviceRegistration.state` is a **derived** property (no enum column):
  QUARANTINED (NULL company) / PRE_REGISTERED / STALE / ONLINE (informed within
  `ACS_STALE_AFTER_SECONDS` = 900).
- Credential binding FKs live **on** the credential row (canon C19). Resolution
  order: inventory_item > device_type > **both NULL = the company default**
  (`tr1_transport_axis` dropped the `network_access_id` tier with its table, and
  replaced it with the unbound-default rung so a tenant-wide SSH/TELNET/WIREGUARD
  credential still has somewhere to live). The tenant's TR-069 Inform credential is
  the one exception to "no other table carries an FK pointing at a credential":
  `provisioning_settings.cwmp_credential_id` / `cwmp_pending_credential_id` name it
  explicitly, because there is exactly one per tenant and the accept-both rotation
  window needs the pair stated rather than inferred.
- **Canonical graph order is leaf → root, everywhere** (doc 35 §3.1): the
  subscriber's own device first, the core last, for every purpose, in the
  executor and in the UI alike. It is the order the traversal produces (no
  re-sort, no second convention), and failure containment is better in both
  directions — on activation a failed CPE step aborts before the OLT is touched;
  on deprovision the subscriber's device is wiped while it is still reachable,
  where core-first would kill the data path and strand the CPE half-configured.
- **One catalog, one identity.** Graph nodes are `InventoryItem` rows and their
  roles come from `device_category`; there is no second node table and no second
  type catalog. That duality is what killed the previous graph
  (`c2d_graph_removal`) and it is not reintroduced.

## Environment Variables
- `CREDENTIALS_KEKS`, `CREDENTIALS_ACTIVE_KEK_ID` — envelope-encryption keys (see
  [utilities.md](utilities.md), `crypto.py`).
- `POSTGRES_*` — Database connection.
</content>
