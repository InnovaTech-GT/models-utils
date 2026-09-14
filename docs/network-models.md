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
| `DeviceCredential` | `device_credential` | name, kind (CHECK: CREDENTIAL_KINDS), username, `secret_ciphertext`/`dek_wrapped`/`kek_id`, fingerprint, binding FKs (inventory_item/device_type/network_access) | Envelope-encrypted per-tenant device secret (canon C1/C19). Secret never round-trips — Out schema exposes only `has_secret` + fingerprint |
| `NetworkAccess` | `network_access` | name, kind (CHECK: acs\|olt), mode (CHECK: direct\|vpn\|tunnel\|nat_zt\|nat_public), is_default, mgmt_subnets (JSON CIDRs), acs_base_url, **`gateway_host`** (String, nullable — NAT transport, 2026-08-13), **`pylon_socks5`** (String, nullable — per-tenant Pylon SOCKS5 endpoint, 2026-08-17) | Per-tenant transport config (canon C9). `mgmt_subnets`-based longest-prefix match was never implemented; `transport.py::resolve_endpoint` reads only the default `kind='olt'` row — see below |
| `AcsDeviceRegistration` | `acs_device_registration` | serial_number, oui, company_id (**nullable** = QUARANTINED), genieacs_device_id, first/last_inform_at, cwmp_cr_* connection-request creds | Serial/OUI→tenant mapping — the tenant-stamping keystone (canon C13); global `(oui, serial)` unique so two tenants can't claim one CPE |
| `ProvisioningSettings` | `provisioning_settings` | company_id (unique), enabled (default **false**), default_inform_interval | Tenant provisioning enable gate — a per-tenant singleton (canon C6). Absence of a row = DISABLED (fail-safe) |
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
- `device_lock_key` (str) — per-device serialization key.
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
`lc2_retire_susp_react` → **`ng1_network_graph`** → **`ng2_topology_drop`** (head).

### The tree lives on `inventory_item` (ng1, doc 35 §2.1)

There is no new node entity — a network element **is** an `InventoryItem` that
has been attached to the graph. Everything provisioning needs is already keyed by
`inventory_item.id` (`mgmt_host`/`mgmt_port`/`cli_protocol`, the device type and
its category, credential bindings, `network_access_id`, the ACS registration,
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
| `plan` | JSON NOT NULL — the ordered subset that will actually be configured, leaf → root: `[{item_id, playbook_id, category_key}]` |
| `frames` | JSON NOT NULL — `{"shared": {...}, "device": {item_id: {...}}}`, resolved **once** at run creation. Later children are built from this rather than re-resolved, so a re-parent landing mid-run cannot silently redirect the remaining steps to devices the operator never saw |
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
- `run_position` — INTEGER NULL, the 0-based index into `ProvisioningRun.plan`,
  i.e. leaf → root order.

Children are created **lazily, one at a time**, so at most one child of a run is
QUEUED or RUNNING at once. That needed no new `ProvisioningJobStatus` value (a
"BLOCKED" state would have had to be understood by every status consumer across
three services) and no change to the worker's claim query — it still picks the
oldest QUEUED job, it simply never sees a child that does not exist yet. What
this buys: the per-device lock is finally correct (each child locks exactly the
device it configures), retry and cancel become per-device, and `PENDING_INFORM`
applies to the CPE child alone instead of stalling the whole path. The mechanics
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
   `workflow_template.definition` get the `ENQUEUE_PROVISIONING` config key
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

## NAT transport (`nat1_gateway_transport`, 2026-08-13)

Second reachability path alongside `direct` (Phase 1) and the reserved
`vpn`/`tunnel` rows (doc 34 §5.5): the tenant maps external ports on their own
gateway to their devices' real management ports (`dst-nat`), and Uplink dials
the gateway instead of the device's private address. Purely additive — no
existing `network_access` row's `mode` changes, and `downgrade()` refuses if
any row is in a NAT mode (see the migration's module docstring for why
silently rewriting to `direct` would strand `gateway_host`/`nat_port` data).

| Table | New columns | Purpose |
|---|---|---|
| `network_access` | `gateway_host` (String, nullable) | The tenant gateway's address on the path **we** dial — a ZeroTier address under `nat_zt`, a public IP or DDNS hostname under `nat_public`. Deliberately `String`, not `INET`: a `nat_zt` value is RFC1918, a `nat_public` value may be a hostname, so no "globally routable" assertion is made. NULL on every non-NAT row |
| `inventory_item` | `nat_port` (Integer, nullable, CHECK 1–65535, unique per `company_id` where set) | The external port on the tenant's gateway that `dst-nat`s to this device. **Never** conflated with `mgmt_port`, which stays the device's real service port |
| `inventory_item` | `mgmt_host_key` (String, nullable) | Pinned SSH host key (TOFU — recorded on first successful connect). Any later mismatch is a hard, non-retryable failure, never an auto-add — see backend-erp's wiki for the pre-auth enforcement mechanism |
| `network_access` | `pylon_socks5` (String, nullable, revision `nat3_pylon_socks5`, 2026-08-17) | The tenant's own Pylon SOCKS5 listener (`"host:port"`), CHECK-required when `mode='nat_zt'`. Replaces a worker env var (`PYLON_SOCKS5`, spec N4 — retracted) after doc 34 OV17 established that one Pylon process joins exactly one ZeroTier network, so a shared fleet proxy can't serve more than one tenant |

`mgmt_port` also gains the range CHECK it had lacked since `nc2a` (a pre-existing gap the xlsx importer could exploit by writing 0 or 70000).

A follow-up revision, `nat2_gateway_host_check`, adds `ck_network_access_nat_gateway_host` (`mode NOT IN ('nat_zt','nat_public') OR gateway_host IS NOT NULL`) — the "gateway_host required for NAT mode" rule was originally enforced only by `NetworkAccessCreate`'s Pydantic validator, which a mode-flipping UPDATE on an existing row could bypass entirely. This is the layer that can't be.

`NETWORK_ACCESS_MODES` widens to `("direct", "vpn", "tunnel", "nat_zt", "nat_public")`; `NAT_MODES = ("nat_zt", "nat_public")` is the subset the resolver treats specially. Both variants resolve the dial target to `(network_access.gateway_host, inventory_item.nat_port)` — they differ only in how the *gateway itself* is reached: `nat_zt` through the tenant's own ZeroTier SOCKS5 proxy (Pylon), `nat_public` over plain internet egress.

**`nat_zt` is dial-capable as of `nat3_pylon_socks5` (2026-08-17).** Doc 34 OV17 retracted the original shared-fleet-Pylon design: one Pylon `refract` process joins exactly one ZeroTier network, so a single Pylon cannot serve `nat_zt` for more than one tenant — it's one Pylon Railway service per tenant. `network_access.pylon_socks5` carries each tenant's own Pylon SOCKS5 endpoint (replacing the earlier `PYLON_SOCKS5` worker env var, spec N4 — retracted), and `database_utils/utils/transport.py::resolve_endpoint` reads it off the tenant's `NetworkAccess` row, failing closed with `PYLON_NOT_PROVISIONED` if it's blank/NULL. `nat_public` remains fully implemented and dial-capable — see [utilities.md](utilities.md) for `resolve_endpoint`'s full resolution logic.

## Open value sets (CHECK-constrained strings, not PG enums)

Following the c3a/c3b precedent, driver-bounded value sets are CHECK-constrained
strings so adding a value is a plain transactional `ALTER` of the CHECK, never the
`ALTER TYPE … ADD VALUE` autocommit dance:

- `CREDENTIAL_KINDS`: SSH, TELNET, SNMP_COMMUNITY, TR069_CONNECTION_REQUEST, HTTP_BASIC,
  HTTP_BEARER, WIREGUARD, AGENT
- `NETWORK_ACCESS_KINDS`: acs, olt · `NETWORK_ACCESS_MODES`: direct, vpn, tunnel, nat_zt, nat_public
  (`NAT_MODES` = nat_zt, nat_public — 2026-08-13, `nat1_gateway_transport`, see the NAT transport
  section above)
- **Cycle 7**: `DEVICE_CATEGORY_TIERS`: CORE, EDGE · `CLI_PROTOCOLS`: ssh, telnet ·
  `INSTALL_STATES`: NOT_INSTALLED, IN_PROGRESS, INSTALLED (SQL CHECK fragments kept
  byte-identical between `models/isp.py` and the nc2a migration, guarded by
  `tests/test_core_config_constants.py`)

## Connections to Other Components
- **backend-erp** — the `genieacs` driver, worker loops (`acs_sync`, PENDING_INFORM
  poller, lease reaper), blast-radius gates, and the network-config routers consume all
  of these. Credentials are decrypted via `database_utils/utils/crypto.py`.
- **auth-erp `Company`** — back-populates `device_credentials`, `network_accesses`,
  `acs_device_registrations`, `provisioning_settings`, `device_action_logs` (all
  `ondelete=CASCADE`).
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
- Credential binding FKs live **on** the credential row (canon C19); no other table
  carries an FK pointing at a credential. Resolution order: inventory_item >
  device_type > network_access default.
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
