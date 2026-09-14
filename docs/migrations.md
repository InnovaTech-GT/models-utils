# Database Migrations

## Description

Alembic-managed schema migrations for all models in this repo — revisions in
`alembic/versions/` (head: **`6e7506e57be9`**) — plus the idempotent seed
scripts that run after every upgrade.

## Goal

Provide a safe, versioned, automated migration path for the single shared
PostgreSQL database across local development and production.

## How migrations run

- **Local development**: the `migrate` service in the root
  `docker-compose.yml` builds this repo's `Dockerfile` and runs
  `alembic upgrade head` on every `docker compose up` (Railway dev was
  decommissioned; local compose is the only dev migration path)
- **Production**: GitHub Actions (`.github/workflows/migrate.yml`) runs
  `alembic upgrade head` against the prod `secrets.DB_URL` on push to `main`.
  The workflow is path-filtered on `alembic/**` (widened to include `env.py`
  and seeds) — so **every seed change must ship with a possibly-no-op
  revision** to trigger it
- **CI migration guard** (`.github/workflows/ci.yml`): PRs that change
  `database_utils/models/**` without adding an `alembic/versions/**` file are
  rejected

## Workflow

1. Modify the model in `database_utils/models/` (on a feature branch from `main`)
2. `alembic revision --autogenerate -m "description"` (needs a reachable DB env)
3. Review the generated revision for correctness
4. Commit; compose into `develop` (erp-release), pin backends to the SHA
5. Local `migrate` service applies it on `docker compose up`; GitHub Actions
   applies it to prod on merge to `main`

Full release mechanics: [deployment-production.md](deployment-production.md).

## Connection configuration

`alembic/env.py` imports all four model modules (for autogenerate) and builds
the DB URL itself from `DATABASE_URL`, `DB_URL`, or the composed `POSTGRES_*`
env vars — the URL is **not** hardcoded in `alembic.ini`.

## Seeds

After `upgrade`, `env.py` runs `_run_seeds(connection)`:

| Seed | Contents |
|---|---|
| `alembic/seeds/rbac_seed.py` | Permissions and roles |
| `alembic/seeds/tier_seed.py` | SaaS tiers |
| `alembic/seeds/isp_seed.py` | ISP permissions, tier modules, purpose-based workflow-template blueprints (+ a convergent retirement pass that sets `is_active = FALSE` on every key in `RETIRED_TEMPLATE_KEYS` — `fiber-cut`, `maintenance`, `service-removal` — never DELETE, so run history survives; it reaches the TEMPLATE row only, not installed tenant copies), device_category baseline (Cycle 7: entries carry a CORE/EDGE tier, column-existence-gated for pre-nc2a positions; a backfill classifies existing rows only while no row has a tier yet, so admin tier edits — including clear-to-NULL — survive re-seeds) |

The modules are importable as `seeds.*` because `env.py` adds the alembic dir to
`sys.path`. All seeds are idempotent (ON CONFLICT / upsert), so re-runs converge
even after SaaS-admin edits.

`scripts/resync_billing_cents.sql` is an ad-hoc billing cents resync helper
(not part of the Alembic chain).

## Notable revision chains

Base revision: `f612571eaad0_initial_schema_with_uuid` (the schema is UUID-native
from the start).

- **Cycle 1 (billing rework)**: `c1a_billing_ddl` → `c1b_backfill` (data
  backfill) → `c1c_payment_ledger` → `c1e_install_actions` → `c1f_verify_grandfather`
- **Cycle 2 (entity merge / topology)**: `c2a_catalog_merge` →
  `c2b_service_billing` (client_service absorbs recurring_order) →
  `c2c_topology_device_chain_playbook` → `c2d_graph_removal` → `c2e_step_exec_snapshot`
- **Cycle 3**: `c3a_topology_purpose_playbooks`, `c3b_device_categories_global_table`
- **Cycle 4 (insights)**: `c4a_insights_dashboards` → `c4b_drop_installation_address`
- **Cycle 5 (network config)**: `nc1a` (five network tables + `ProvisioningJob`
  columns + `PENDING_INFORM` via `ALTER TYPE … ADD VALUE` + 17 permissions) →
  `nc1b` (append-only `device_action_log` trigger)
- **Cycle 7 (core config)**: `nc2a_core_config` — hand-written (not
  autogenerate), additive, guarded/idempotent with in-migration assertions:
  `device_category.tier` (+ key-based backfill, ONU → 'ONU / ONT' rename),
  `device_type.cli_platform`, the `inventory_item` mgmt surface,
  `topology_device_type.inventory_item_id` (FK SET NULL + index — dropped with
  its table by `ng2_topology_drop`),
  `client_service.install_state`/`installed_at` (+ index); three new CHECK
  constraints whose SQL fragments are kept byte-identical with
  `models/isp.py` (guarded by `tests/test_core_config_constants.py`).
  Fully reversible; backfill UPDATEs are convergent (second run = zero rows)
- **Grandfathered verification**: `t2_grandfather_email_verified` — one-shot backfill marking every pre-overhaul user email-verified so the new login gate cannot lock out existing production users; irreversible by design.
- **Brownfield adoption (doc 30)**: `ba1_attested_adoption` (parent
  `t2_grandfather_email_verified`) — hand-written, nc2a-style
  guarded/idempotent ops with post-upgrade assertions: additive
  `client_service.adopted_at`/`adopted_by_user_id`/`adoption_note` columns +
  FK `fk_client_service_adopted_by_user` (→ `"user"`, SET NULL) + partial
  index `ix_client_service_adopted` (`company_id` WHERE `adopted_at IS NOT
  NULL`) + idempotent `client_services.adopt` permission insert granted to
  the global system ADMIN **only**. Total downgrade (deletes the permission +
  grants, drops index/FK/columns — attestation data is lost on downgrade).
  The seed changes ride this revision: `isp_seed.ADMIN_ONLY_PERMISSIONS` and
  `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS` keep MANAGER excluded at both
  auto-grant sites (subset-pinned by `tests/test_attested_adoption.py`).
- **Client install-field removal (doc 31)**: `cf1_drop_client_install_fields`
  (parent `ba1_attested_adoption`) — hand-written, nc2a/ba1
  house style. **Destructive one-shot** — safe this release only because prod
  is pre-cycle-1: the chain creates the columns in `cd2f0076c709` and drops
  them here in one linear pass. Data cleanup runs BEFORE the DDL: deletes
  `workflow_step` rows whose `UPDATE_FIELD` `action_config` writes
  `installation_status`/`installation_date` (installed new-installation v2
  s3 copies) with edge rerouting (live predecessors → live successors
  through doomed steps, deduped; `workflow_step_execution` rows survive via
  the c2e SET NULL FK + `step_name` snapshot), and deletes clients
  `insight_chart` rows using the `installation_status` dimension/filter
  (deletion over stripping — a stripped spec silently changes meaning).
  Then `ALTER TABLE client DROP COLUMN installation_status/installation_date`
  and `DROP TYPE installationstatus`. Downgrade recreates enum + columns
  (NOT NULL DEFAULT 'NOT_INSTALLED', nullable date) — data NOT restorable.
  Guardrails incl. a quote-agnostic single-head file scan:
  `tests/test_client_install_field_drop.py`. The seed change rides this
  revision: `isp_seed` new-installation v3 drops step s3 + edge s2→s3.
- **Recurrente tenant billing**: `rb1_recurrente_billing` (parent `cf1`) —
  additive only. Adds the `recurrente_*` gateway columns:
  `tier.recurrente_product_id`/`recurrente_price_id`/`recurrente_price_yearly_id`
  (NULL price id = not purchasable online), `company.recurrente_customer_id`
  (lazy, first checkout), `subscription.recurrente_subscription_id` (unique) +
  `recurrente_checkout_id`/`card_last4`/`card_brand`, and
  `billing_invoice.recurrente_intent_id` (unique — webhook charge idempotency).
  Creates the `billing_webhook_event` table (`svix_id` string PK,
  `event_type`, `created_at`) — webhook delivery idempotency log. Fully
  reversible downgrade (drops table + columns).
- **New-installation template v4 (task-context cycle, doc 32)**:
  `tk1_new_installation_v4` (parent `rb1_recurrente_billing`) —
  schema **no-op** (`upgrade()`/`downgrade()` both pass); it exists so the
  path-filtered prod `migrate.yml` workflow fires and replays seeds. The seed
  change rides this revision: `isp_seed` bumps the `new-installation` blueprint
  to v4 — the installation-fee param moves from the retired legacy Product
  catalog to a **service plan** (param type `service_plan`, key
  `installation_fee_plan_id`), and the `CREATE_ORDER` step item uses
  `service_plan_id` (the engine's preferred resolution). The old required
  `product` param blocked fresh tenants entirely (products have no create path
  anymore, so the required product UUID could never be satisfied). Installed v3
  tenant copies keep running — `product_id` items remain
  deprecated-but-honored during the rollback window.
- **Free/Trial unlimited**: `t1_free_trial_unlimited` — data migration; merges `{max_users,max_products,max_clients} = -1` into Free/Trial `tier.features` and grants the full module list. Product decision: free tier has NO limits until further notice. `tier_seed.py` seeds fresh DBs the same way (now also writes `tier.modules`).
- **Free/Trial deactivated (Recurrente paywall)**: `6e7506e57be9` (parent `pi1_payment_idem`) — data-only migration; sets `is_active = false` on the "Free" and "Trial" tiers. Uplink billing now requires Recurrente checkout for every company — no free tier/trial is offered. Rows are not deleted (existing companies/subscriptions may still reference them by FK); the unlimited-features policy above is unaffected. `tier_seed.py` seeds fresh DBs with `is_active: False` for both so a wiped local DB (`docker compose down -v && up --build`) can't resurrect them as assignable.
- **Cycle 8 (topology-owned playbooks)**: `c8a_playbook_topology` —
  hand-written (not autogenerate), nc2a-style guarded/idempotent ops
  (`DROP … IF EXISTS`, `ADD COLUMN IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS`)
  with in-migration assertions verifying each object's final state, so a re-run
  is a no-op. **Destructive**: drops `playbook.target_vendor` and
  `playbook.target_category_id` (+ its FK `fk_playbook_target_category_id` from
  `c3b`) — safe because the consuming backend/frontend ship in the same release
  and Cycles 1–8 have not reached prod. Adds `playbook.topology_id` (UUID FK →
  `topology.id` **ON DELETE CASCADE**, nullable, indexed
  `ix_playbook_topology_id`): NULL = a system/global playbook, non-NULL = an
  inline playbook owned by that topology. No backfill (existing playbooks keep
  `topology_id` NULL until the topology editor re-saves). The CASCADE removes an
  inline playbook when its topology is deleted but does **not** on its own
  guarantee an orphan-free delete — `provisioning_job.playbook_id` is
  `ON DELETE RESTRICT` (NOT NULL, no topology FK), so the backend
  topology-delete path must first clear dependent `provisioning_job` rows; the
  RESTRICT backstop deliberately preserves job history. Downgrade re-adds the
  dropped columns (shape only — a destructive drop's data is unrecoverable) with
  the RESTRICT FK restored, and drops `topology_id`.
  **Half of this is now history**: the `target_vendor`/`target_category_id` drop
  stands, but `playbook.topology_id` (and the `topology` table it referenced) was
  dropped again by `ng2_topology_drop` — playbook ownership lives in the
  `device_type_playbook` / `inventory_item_playbook` binding tables
- **ISP core**: `cd2f0076c709_isp_platform_core_service_plans_`; plus tenant
  indexes (`a1f2b3c4d5e6`), timezone fixes, and task/workflow/integration modules

- **Namespaced playbook variables**: `pv1_namespaced_variables` — a
  DATA-only revision (no DDL). Rewrites every `{{token}}` in
  `playbook.definition` through the flat→namespaced name map, converts
  `service_plan.provisioning_params` from `{"vlan": 110}` to
  `[{key, value, description}]` rows, and prefixes workflow `action_config`
  variable KEYS with `input.` (values may be `{{trigger.*}}` templates, a
  different namespace, and are left alone). Rewritten playbooks get
  `last_dry_run_version = NULL` so machine-edited device config must be
  re-simulated before it runs live. Idempotent: a second run finds no legacy
  tokens and leaves every row byte-identical. **Not reversible** — the flat
  namespace was ambiguous by construction (which is why it was replaced), and
  the retired unique-category aliases (`onu_serial`, …) cannot be recovered at
  all; they are rewritten to a greppable `RETIRED_ALIAS.*` marker so they fail
  loudly instead of resolving to nothing

- **Per-service provisioning parameters**: `sp1_service_params` — purely
  additive, one nullable JSON column `client_service.provisioning_params`
  holding this service's values for the parameters its plan declares with
  `scope='service'`. `service_plan.provisioning_params` is NOT rewritten: its
  rows gain an optional `scope` and a row without one is plan-scoped, which is
  exactly what every pre-feature row is. Reversible (drops the column).

- **Service lifecycle — topology backfill**: `bf1_topology_backfill` (parent
  `sp1_service_params`). **Historical — the column it backfills was dropped by
  `ng2_topology_drop`**, so on any database migrated past `ng2` this revision has
  no lasting effect. Kept in the chain (revisions are never rewritten) and
  documented because a partial upgrade can still stop on it. DATA-only, no DDL
  (both columns ship with
  `c8a_playbook_topology`). Sets `client_service.topology_id` from
  `service_plan.default_topology_id` wherever it is NULL and the plan declares a
  default. Motivation: before this cycle `topology_id` was set only by the create
  path, so every service imported by the adoption campaign (doc 30) or created
  before topologies existed carries NULL — and a NULL topology resolves no
  playbook for any purpose, so the new pre-flight gate blocks
  suspend/reactivate/cancel and DELETE refuses non-cancelled rows. Those services
  are stranded, reachable only via the ADMIN force-cancel hatch. Idempotent: the
  `topology_id IS NULL` predicate makes a re-run a no-op and never overwrites an
  operator who later cleared or re-pointed a topology by hand. **Residuals are
  expected, not a failure** — a service whose plan has no `default_topology_id`
  cannot be repaired by any safe rule (picking an arbitrary topology would
  silently provision the wrong device chain); the before/backfilled/residual
  counts are printed so the operator knows how many rows still need a manual
  assignment from the UI. `downgrade()` is a deliberate no-op: a backfilled
  `topology_id` is indistinguishable from a hand-set one (no marker column), so
  NULLing them back out would destroy real operator assignments and re-strand the
  services this un-stranded.

- **Service lifecycle — retire the 'service-removal' template**:
  `lc1_retire_removal_tmpl` (parent `bf1_topology_backfill`).
  Cancelling a service now natively cancels billing and enqueues the service's
  DEPROVISION playbook(s) from the cancel handler; the `service-removal` template
  did the same thing as a workflow triggered on `client_service.status changed_to
  CANCELLED`, so leaving it live double-fires. **Two distinct things are
  retired:**
  1. The **template row** — by the seed: `service-removal` is removed from
     `WORKFLOW_TEMPLATES` and added to `RETIRED_TEMPLATE_KEYS` in `isp_seed.py`;
     the convergent retirement pass sets `workflow_template.is_active = FALSE`
     (never DELETE — run history stays intact). Seeds run after upgrade via
     `env.py`, and the revision exists at all so the path-filtered prod
     `migrate.yml` fires (same pattern as `tk1_new_installation_v4`).
  2. The **installed per-tenant `workflow` rows** — by `upgrade()` itself, and
     *not* by the seed. Installing a template materializes an INDEPENDENT
     `workflow` row: there is no `template_id`/key column on `workflow`, and
     `find_matching_workflows` filters on `Workflow.is_active` alone and never
     joins `workflow_template`. Deactivating the template therefore has zero
     effect on tenants who already installed it. (Precedent:
     `c2d_graph_removal` step 2.)

  Why a stale copy is a correctness bug and not merely redundant: on a NORMAL
  cancel the native job is already QUEUED when triggers fire, so the shared
  `deprovision-{client_service_id}` idempotency key absorbs the duplicate. But an
  **ADMIN force-cancel** (`force:true`, used when the service's path resolves no
  DEPROVISION playbook) deliberately enqueues NOTHING and audit-logs that fact — there is no
  native job for the key to collide with, so the stale workflow fires a
  deprovision against live equipment, violating the exact guarantee force-cancel
  exists to make.

  **Targeting is behavioural, not provenance-based.** A workflow is deactivated
  iff it is currently active AND (a) it has a `client_service`/`UPDATED` trigger
  whose `field_conditions` are `status changed_to CANCELLED`, AND (b) it has an
  `ENQUEUE_PROVISIONING` step whose `action_config` either names purpose
  `DEPROVISION`, or carries a `deprovision-%` `idempotency_key`, or **names no
  purpose at all**. The last arm is required: installed workflows are FROZEN
  copies taken at install time and never converge to a later template version, so
  a tenant who installed before the Cycle-3 purpose gate and before v4 added an
  idempotency key holds a step with neither field — and those are the worst to
  miss, since with no idempotency key they double-enqueue on a normal cancel too.

  **CAVEAT / RELEASE NOTE (the schema records no provenance):** this predicate
  cannot distinguish an installed `service-removal` copy from a **hand-built
  tenant workflow of the same shape**, and will deactivate that one too. This is
  accepted on the merits — any active workflow that enqueues a DEPROVISION on
  `status changed_to CANCELLED` is both redundant with the native cancel handler
  and the force-cancel hazard above, regardless of author. The match is kept
  narrow (both conditions required; only `ENQUEUE_PROVISIONING`/DEPROVISION steps
  count) so a tenant workflow that merely *reacts* to cancellation — emails the
  customer, closes a task, opens a ticket, updates billing — fails condition (b)
  and is untouched. Tenants who had installed *Service Removal* will find it
  deactivated; cancelling still cancels billing and runs the DEPROVISION playbook
  natively, so no tenant action is needed. A hand-built workflow can be re-enabled
  from the automations UI; the affected workflow ids are printed by the migration.

  Re-runnable (statements only ever narrow to `is_active = TRUE`; sets
  `lock_timeout = '5s'`). `downgrade()` is a no-op: the deactivated ids are not
  persisted beyond the migration log, and a blanket reactivation would re-enable
  workflows tenants had deliberately turned off (same posture as
  `c2d_graph_removal`).

- **Service lifecycle — retire the 'suspension'/'reactivation' templates**:
  `lc2_retire_susp_react` (parent `lc1_retire_removal_tmpl`). The sibling of
  `lc1`, same mechanism and same reasoning: both templates have the identical
  shape (trigger on a `client_service` status change, then
  `ENQUEUE_PROVISIONING` with the purpose-resolution mode), and the lifecycle
  endpoint now enqueues those playbooks directly — so leaving them installed
  double-fires a device operation on every suspend and every reactivate. Both
  halves are redundant: the billing step is done natively by
  `_apply_suspension`/`_apply_reactivation` in backend-erp (verified against the
  handlers before the revision was written — had the native path not resumed
  billing, retiring 'reactivation' would have silently broken billing
  resumption), and the provisioning step is superseded by the endpoint. Relying
  on the templates' idempotency keys instead would rest on two string literals in
  different repos staying byte-identical forever, and does not hold at all for
  the pre-v4 installed shape, which has no key.

### Cycle 10 — the company network graph (doc 35)

Two revisions on `lc2_retire_susp_react`, deliberately split so a reviewer can
read "what appears" and "what disappears" independently. Full structural detail
in [network-models.md](network-models.md).

- **`ng1_network_graph`** — strictly **additive**: nothing is dropped, nothing is
  rewritten, nothing is even read. Adds `inventory_item.parent_id` (self-FK
  RESTRICT) + `network_attached` with two CHECKs and two indexes;
  `device_category.is_passive`; the `device_type_playbook` and
  `inventory_item_playbook` binding tables; `client_service.cpe_item_id` +
  `path_changed_at`; the `provisioning_run` table plus
  `provisioning_job.run_id`/`run_position`; and the two plpgsql guards
  `trg_inventory_item_graph_guard` (self-parent, cross-tenant parent, detached
  parent, cycle, depth ≥ 32) and `trg_inventory_item_detach_guard` (detaching a
  node that still has children). Reusing the existing `provisioningjobstatus` /
  `provisioningtrigger` PG enums needs `PGEnum(..., create_type=False)` — a plain
  `sa.Enum` would try to `CREATE TYPE` and fail with DuplicateObject. The
  triggers and the two purpose-format CHECKs live **only in the revision**, never
  in SQLAlchemy metadata: consuming test suites build schemas with SQLite
  `create_all`, which parses neither plpgsql nor the PG regex operator `~`
  (precedent: `ck_topology_playbook_purpose_format`, `nc1b`).

- **`ng2_topology_drop`** (**head**) — the destructive half, three phases in this
  order and no other, and **irreversible**: `downgrade()` raises
  `NotImplementedError` because a graph cannot be turned back into a set of named
  chains (they carried per-topology playbook bindings and pinned positions the
  graph does not encode).
  1. **Guards, before anything is touched.** Raise on any `playbook.definition`
     still containing `chain[`, `edge_devices[`, `core_devices[`,
     `RETIRED_ALIAS` or `target_position` (listing the offending ids), and on any
     `client_service` with `topology_id IS NOT NULL AND cpe_item_id IS NULL`.
     *Why raise rather than repair:* doc 35 forbids a compatibility shim, and a
     playbook still written against `chain[n]` would not fail loudly at run time
     — the renderer guard catches the unrendered token only after the job has
     been queued, claimed and partially executed. Stopping the release is cheaper
     than discovering it on a customer's OLT. And **there is deliberately no
     chain → graph backfill**: a chain names device *types*, a graph names device
     *instances*; deriving one from the other would invent parent edges and
     fabricate physical facts about someone's plant.
  2. **Idempotent rewrite.** `workflow_step.action_config` and
     `workflow_template.definition`: the `ENQUEUE_PROVISIONING` config key
     `"use_topology"` → `"use_service_path"`, predicate-guarded
     (`WHERE ... LIKE '%use_topology%'`) so a second run matches nothing and
     leaves every row byte-identical. Only the key changes.
  3. **Drops**, columns before tables (a referencing FK would block
     `DROP TABLE topology`): `client_service.topology_id` + its index,
     `service_plan.default_topology_id`, `playbook.topology_id`, then
     `topology_playbook`, `topology_device_type`, `topology`. Every step is
     existence-guarded, so a re-run is a no-op.

  Production (verified 2026-08-06) is at `a1f2b3c4d5e6` with 38 tables and **no
  ISP schema at all**, so both guards are vacuous there — the tables they inspect
  are created empty by earlier revisions in the same release chain. They exist
  for the local/staging databases carrying Cycles 1–9 data.

  Seed side: `isp_seed.DEVICE_CATEGORIES` rows widen to
  `(key, name, sort_order, tier, is_passive)`, and the passive classification
  follows the `tier` precedent exactly — it fires only while **no** row anywhere
  is classified, so a super-admin who deliberately marks a splitter active (a
  tenant with managed splitters reporting optical power would) is never reverted
  on the next migrate.

### NAT transport (`nat1_gateway_transport`, 2026-08-13)

On `ng2_topology_drop`. Purely **additive** — no existing row's `mode`
changes; Cable Santa Rosa (the live production tenant) keeps whatever mode it
already has. Full column/constraint detail in
[network-models.md](network-models.md#nat-transport-nat1_gateway_transport-2026-08-13).

- Adds `network_access.gateway_host` (String, nullable), `inventory_item.nat_port`
  (Integer, nullable, range-CHECKed) and `inventory_item.mgmt_host_key` (String,
  nullable).
- Drops and recreates `ck_network_access_mode` to widen the CHECK to include
  `nat_zt`/`nat_public`.
- Clamps any pre-existing out-of-range `mgmt_port` to NULL **before** adding
  `mgmt_port`'s own new range CHECK (`ck_inventory_item_mgmt_port`) — `mgmt_port`
  has had no range CHECK since `nc2a`, and the xlsx importer would happily have
  written 0 or 70000.
- Adds a partial unique index `uq_inventory_item_company_nat_port` on
  `(company_id, nat_port)` where `nat_port IS NOT NULL` — a tenant has one
  gateway, so two devices behind one external port would push a config to the
  wrong device.
- `downgrade()` **refuses** rather than silently rewriting NAT rows to
  `direct`: it raises `RuntimeError` if any `network_access` row is in
  `nat_zt`/`nat_public` mode, because that would strand `gateway_host` and
  every device's `nat_port` in columns the downgrade then drops, and the next
  upgrade would come back with `mode='direct'` pointing at a management LAN
  nothing can reach. Switch the affected tenants off NAT explicitly first.
- The CHECK fragments (`_NETWORK_ACCESS_MODE_CHECK`, `_NAT_PORT_CHECK`,
  `_MGMT_PORT_CHECK`) are duplicated byte-for-byte between
  `database_utils/models/isp.py` and the migration (the nc1a/nc2a precedent —
  revisions are immutable, models are not), pinned equal by
  `tests/test_nat_transport_constants.py`.

### `nat2_gateway_host_check` (2026-08-13)

On `nat1_gateway_transport`. Additive, hand-written: adds DB-level CHECK
`ck_network_access_nat_gateway_host` (`mode NOT IN ('nat_zt','nat_public') OR
gateway_host IS NOT NULL`), closing the gap where `NetworkAccessUpdate` had no
cross-field validator and a mode-flipping UPDATE could bypass the Pydantic
check entirely. Scrubs any pre-existing NAT row with no `gateway_host` back
to `direct` before adding the constraint.

### `nat3_pylon_socks5` (2026-08-17, head)

On `nat2_gateway_host_check`. Adds `network_access.pylon_socks5` (String,
nullable) — the tenant's own Pylon SOCKS5 endpoint — plus DB-level CHECK
`ck_network_access_pylon_socks5` (`mode != 'nat_zt' OR pylon_socks5 IS NOT
NULL`). Doc 34 OV17 retracted the original shared-fleet-Pylon design (one
Pylon process joins exactly one ZeroTier network, so it can't serve more than
one tenant); the SOCKS5 endpoint moves from a worker env var (`PYLON_SOCKS5`,
spec N4 — retracted) to this per-tenant column, mirroring `gateway_host`. No
production tenant has ever run `nat_zt` (it has fail-closed since `nat1`
shipped, since `PYLON_SOCKS5` was never set), so the same clamp-before-CHECK
scrub as `nat2` is defensive rather than expected to fire.
`downgrade()` drops the column and its CHECK cleanly (no data-loss ambiguity
like `nat1`'s mode downgrade) — a `nat_zt` tenant on a downgraded schema has
no proxy column left to read and fails closed on the transport channel.

## Key rules

- **Not all migrations are reversible**: `c1e_install_actions` uses
  `ALTER TYPE ... ADD VALUE`, which has no downgrade, and `ng2_topology_drop`
  raises from `downgrade()` by design. `nat1_gateway_transport`'s `downgrade()`
  is conditionally reversible — it raises only while a `network_access` row is
  still in a NAT mode. Check each revision's `downgrade()` before assuming
  rollback is possible
- Additive changes (new columns/tables): safe to apply before consuming
  service code ships
- Destructive changes (removing/renaming): apply AFTER all consuming service
  code is in production
- Parallel schema features use separate branches/revisions — never combine
  unrelated schema changes

## Environment Variables

- `DATABASE_URL` / `DB_URL` / `POSTGRES_USER`+`POSTGRES_PASSWORD`+`POSTGRES_HOST`+`POSTGRES_PORT`+`POSTGRES_DB` — connection for `alembic/env.py`
