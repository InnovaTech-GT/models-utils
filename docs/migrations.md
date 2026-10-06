# Database Migrations

## Description

Alembic-managed schema migrations for all models in this repo — revisions in
`alembic/versions/` (head: **`pt2_unmap_port_labels`**) — plus the idempotent seed
scripts that run after every upgrade.

## Goal

Provide a safe, versioned, automated migration path for the single shared
PostgreSQL database across local development and production.

## How migrations run

- **Local development**: the `migrate` service in the root
  `docker-compose.yml` builds this repo's `Dockerfile` and runs
  `alembic upgrade head` on every `docker compose up`
- **Railway development**: GitHub Actions (`.github/workflows/migrate.yml`)
  runs `alembic upgrade head` against the `development` environment's
  `secrets.DB_URL` (the Railway dev Postgres public URL) on push to `develop`
- **Production**: the same workflow job runs against the `production`
  environment's `secrets.DB_URL` on push to `main`. One job, environment
  chosen by branch, a per-branch concurrency group, and `workflow_dispatch`
  for manual re-runs.
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
| `alembic/seeds/isp_seed.py` | ISP permissions, tier modules (the workflow-template catalog + retirement pass were removed by `ld1_legacy_drop`), device_category baseline (Cycle 7: entries carry a CORE/EDGE tier, column-existence-gated for pre-nc2a positions; a backfill classifies existing rows only while no row has a tier yet, so admin tier edits — including clear-to-NULL — survive re-seeds) |

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
- **Cycle 4 (insights)**: `c4a_insights_dashboards` → `c4b_drop_installation_address`; **Insights v2**: `iv1_insights_v2` (see below)
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
  `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS` kept MANAGER excluded at both
  auto-grant sites. (Both tuples were removed with MANAGER in
  `rr1_four_builtin_roles`; ADMIN-only now means "no ISP_ROLES grant and not a
  `read` action", pinned by `tests/test_attested_adoption.py`.)
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
  `_MGMT_PORT_CHECK`) were duplicated byte-for-byte between
  `database_utils/models/isp.py` and the migration (the nc1a/nc2a precedent —
  revisions are immutable, models are not), pinned equal by
  `tests/test_nat_transport_constants.py`. Since `tr1_transport_axis` only
  `_NAT_PORT_CHECK`/`_MGMT_PORT_CHECK` still have a model side (`inventory_item`
  is untouched); `_NETWORK_ACCESS_MODE_CHECK` lives on ONLY inside this immutable
  revision, and the test pins the two surviving pairs plus each revision's chain
  position.

### `nat2_gateway_host_check` (2026-08-13)

On `nat1_gateway_transport`. Additive, hand-written: adds DB-level CHECK
`ck_network_access_nat_gateway_host` (`mode NOT IN ('nat_zt','nat_public') OR
gateway_host IS NOT NULL`), closing the gap where `NetworkAccessUpdate` had no
cross-field validator and a mode-flipping UPDATE could bypass the Pydantic
check entirely. Scrubs any pre-existing NAT row with no `gateway_host` back
to `direct` before adding the constraint.

### `nat3_pylon_socks5` (2026-08-17)

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

### `fg1_integration_enabled_regby` (2026-09-17)

On `ng2_provisioning_run_list`. Figma Settings follow-ups, additive and hand-
written (lock_timeout, idempotent guards, post-upgrade assertions, total
downgrade): `integration.enabled` (BOOLEAN NOT NULL DEFAULT true — "disconnect"
without losing credentials; backend-erp refuses disabled integrations),
`integration.provider` (VARCHAR NULL, backfilled `WHATSAPP_BUSINESS` where
`base_url ILIKE '%graph.facebook.com%'`), and
`acs_device_registration.created_by_user_id` (UUID NULL, FK
`fk_acs_device_registration_created_by_user` → `"user"(id)` ON DELETE SET
NULL — NULL for bootstrap/quarantine and legacy rows). The id is short on
purpose: `alembic_version.version_num` is VARCHAR(32), and a longer id fails
the version stamp after the DDL has run (the transaction rolls back).

### `dc1_category_trim` (2026-09-17)

On `fg1_integration_enabled_regby`. USER DECISION: the global device-category
list is trimmed to the six keys backend-erp seeds as every new tenant's
default products (`utils/inventory_defaults.py`) — ROUTER, SWITCH, OLT, ONU,
FIBER_OPTIC, PATCH_CORD. The other 16 baseline keys are set `is_active =
false`, never dropped — `device_type.category_id` is a RESTRICT FK and
provisioning tasks reference categories by key, so a hard delete would break
existing rows. Idempotent (plain `UPDATE ... WHERE key = ANY(...)`, safe to
re-run), post-upgrade assertion that exactly the six are active and none of
the sixteen are. `downgrade()` reactivates all 22 (the pre-trim state).
`isp_seed.DEVICE_CATEGORIES` gained a 7th element (`is_active`) so a fresh
insert on a brand-new database already lands in the trimmed state instead of
depending on this migration ever having run against it.

### `iv1_insights_v2` (2026-09-18)

On `dc1_category_trim`. Insights v2 persistence (uplink-workspace spec
`docs/superpowers/specs/2026-09-18-insights-v2-design.md` §5.1). Hand-written
(autogenerate cannot see enum label additions), in ba1/tj1/pm1 house style:
`SET lock_timeout = '5s'`, then `ALTER TYPE insightcharttype ADD VALUE IF NOT
EXISTS 'LINE'` inside `op.get_context().autocommit_block()`, then
`insight_dashboard.default_time_range` and `insight_chart.viz` as `JSON NULL`
(`ADD COLUMN IF NOT EXISTS`). Post-upgrade assertions raise `RuntimeError` if
the label is missing from `pg_enum` or either column is not `json` in
`information_schema.columns`. No backfill: no v1 chart existed anywhere.

`downgrade()` refuses (`RuntimeError`) while any `insight_chart.chart_type` is
`LINE`, because the pre-v2 Python enum cannot load such a row. Otherwise it drops
the two columns. The `LINE` label stays: a documented no-op, because PG cannot drop
enum labels (precedents `c1e`, `nc1a`, `pm1`, `tj1`). Verified on PG 16 with
upgrade → guarded downgrade → downgrade → re-upgrade on a scratch database.
Guardrails: `tests/test_insights_v2.py`.

### `vpn1_vpn_socks5` (2026-09-25)

On `iv1_insights_v2`. Adds `network_access.vpn_socks5` (String, nullable) — the
tenant's own WireGuard-hub SOCKS5 listener — plus CHECK
`ck_network_access_vpn_socks5` (`mode != 'vpn' OR vpn_socks5 IS NOT NULL`).
Shape copied from `nat3_pylon_socks5`, but the mode dials `item.mgmt_host`
DIRECTLY: the hub holds a real kernel route into the tenant LAN via WireGuard,
so `vpn_socks5` is only the proxy hop and never replaces `mgmt_host` the way
`gateway_host` does under `NAT_MODES`. (Historical: `tr1_transport_axis` dropped
this column with the table. The behaviour it describes is now
`dial_target='device'` + `proxy_kind='socks5'`.)

Authored by Mario Cano as `tun1_tunnel_socks5` (mode `tunnel`, column
`tunnel_socks5`) and renamed here: the mode he built and lab-validated IS canon
C17's `vpn` (WireGuard + SOCKS5), assembled from an external VPS hub instead of
the in-container userspace wireproxy C17 specced. `tunnel` stays reserved for
canon C10's edge agent and stays in backend-erp's `_UNSHIPPED_MODES`. The
original was also parented on `ng2_provisioning_run_list`, an interior node that
already had a child, so merging it forked the graph and `alembic upgrade head`
aborted with `Multiple head revisions are present` — re-parented onto the real
head. **No mode-CHECK change:** `vpn` was already in `NETWORK_ACCESS_MODES` and
`ck_network_access_mode`.

Unlike `nat2`/`nat3` the clamp (`UPDATE network_access SET mode='direct' WHERE
mode='vpn' AND vpn_socks5 IS NULL`) is a REAL backfill, not a defensive no-op:
`vpn` has been API-creatable since `nc1a` while `resolve_endpoint` had no vpn
branch. It also has to run BEFORE any backend carrying the new schema deploys —
`NetworkAccessOut` inherits `NetworkAccessBase`'s validator, so a proxy-less
`vpn` row would otherwise 500 every `GET /network-access/` for that tenant.
That is a second, independent reason the models-utils-first push order is not
optional. `downgrade()` drops the column and CHECK cleanly. Guardrails:
`tests/test_vpn_transport_constants.py`.

### `na1_kind_outbound` (2026-09-25)

On `vpn1_vpn_socks5`. Renames the `network_access.kind` value `olt` to
`outbound` — the row was never OLT-specific, it is the tenant's default
OUTBOUND path for every managed device.

**The CHECK is SWAPPED, not widened**: `ck_network_access_kind` becomes
`kind IN ('acs','outbound')`, the rows are rewritten (`UPDATE ... WHERE
kind='olt'`), and a post-upgrade assertion raises `RuntimeError` if any `olt`
survives. `'olt'` is no longer a legal value on any path — write, read or
stored.

An earlier draft of this revision was additive (widen now, narrow next cycle)
because `NetworkAccessOut` inherits `NetworkAccessBase.validate_kind`, so a
backend still pinned to the previous models-utils raises on every
`network_access` READ of a rewritten row; models-utils must migrate FIRST (the
additive columns in `vpn1`/`ac1` are SELECTed by the new ORM), while a rename
normally demands consuming code first (the workspace pitfall "removing or
renaming: all consuming service code must be in production FIRST"). That
conflict only bites if rows exist. Both the Railway `development` and the
production databases were checked before this revision was finalised:
`network_access` holds **zero** rows in both and both sit at
`alembic_version = iv1_insights_v2`, so there is no row to poison and no window
to protect. The `UPDATE` is kept anyway — harmless on both, and correct for a
developer's local database that does hold an `olt` row. If `network_access`
ever holds live rows again, the safe sequence for a value rename is the old
one: widen, deploy every consumer, rewrite, narrow.

Order inside `upgrade()` is load-bearing — neither CHECK admits both spellings,
so the constraint is dropped, the rows are rewritten, and only then is the
narrow CHECK created. `uq_network_access_default` (UNIQUE
`(company_id, kind)` WHERE `is_default`) needs no recreation: it indexes the
column, and an in-place value UPDATE preserves uniqueness (no `outbound` row
could pre-exist). `nc1a_network_config_core.py`'s fragment copy stays
`('acs','olt')` — immutable, and correct for the schema as of `nc1a`; a fresh
database migrates `nc1a -> ... -> na1` and ends correct.

### `ac1_acs_tenant_auth` (2026-09-25)

On `na1_kind_outbound`. Capa 3: a CPE identifies itself to GenieACS by serial
number alone — printed on its label — so tenant attribution rests on public
data. This revision carries the schema half of the credential proof. Attribution
itself stays serial-derived (no GenieACS patching); the password only
authenticates it.

- `network_access.acs_auth_required` BOOLEAN NOT NULL `server_default false`.
  Default-OFF is expressed as a DB constraint, not app code: OFF means ALLOW, so
  a tenant that never enrols behaves exactly as today. **`tr1_transport_axis`
  moved this column to `provisioning_settings`, name and semantics unchanged.**
- CHECK `ck_network_access_acs_auth_required` (`kind = 'acs' OR
  acs_auth_required = false`) — the gate is only read off the tenant's default
  `kind='acs'` row, and the CHECK stops a raw UPDATE arming it where nothing
  looks. **Not recreated by `tr1`: on a singleton there is no wrong row.**
- Partial UNIQUE `uq_acs_registration_serial_no_oui` on
  `acs_device_registration (serial_number) WHERE oui IS NULL`. A multi-tenancy
  fix, not housekeeping: `uq_acs_registration_identity` is a plain two-column
  UNIQUE, Postgres treats NULLs as distinct, `oui` is nullable and
  `_normalize_oui` returns `None` unchanged for an omitted OUI — so `(NULL,
  serial)` can repeat today and the router's 409 is check-then-insert with no DB
  backstop. Once the gate is armed, a duplicated serial lets the inform-auth
  lookup's `.first()` hand one tenant's CWMP password to another tenant's CPE.
  Built behind a pre-check that names the offending serials rather than failing
  the release with a bare index-build error.
- The `device_credentials.reveal` permission row (no role grant — ADMIN-only, and ADMIN comes from the convergent seed), cfg3 recipe
  (idempotent `INSERT ... ON CONFLICT (name) DO NOTHING`, per-role grant,
  post-upgrade count assertion, total `downgrade()`). ADMIN comes from the
  convergent seed. (MANAGER, and the exclusion tuples that withheld this from
  it, were removed by `rr1_four_builtin_roles`; VIEWER's convergent grant only
  matches `read` actions, so `reveal` stays ADMIN-only —
  `tests/test_vpn_transport_constants.py` pins it.)

**No `pending_*` columns, and deliberately no unique index on `(company_id,
network_access_id)`.** The accept-both rotation window is a SECOND
`DeviceCredential` row bound to the same `acs` row (`informPassword` = newest,
`informPendingPassword` = second-newest; rotation is create-new -> roll out ->
delete-old, and `POST /{id}/rotate` is not used for this credential). Such an
index would forbid exactly that row. **`tr1_transport_axis` keeps the two-row
shape and replaces the newest/second-newest INFERENCE with two explicit FKs,
`provisioning_settings.cwmp_credential_id` / `cwmp_pending_credential_id`.**

Verified on PG 16 on a scratch database: `upgrade head` -> `downgrade
iv1_insights_v2` -> `upgrade head` -> `downgrade`, with a seeded legacy row
proving both data steps (a proxy-less `vpn` row clamped to `direct` by `vpn1`,
an `olt` row rewritten to `outbound` by `na1` and back again on downgrade).
Guardrails: `tests/test_vpn_transport_constants.py`.

### `ld1_legacy_drop` (2026-10-02, head) - DESTRUCTIVE, models-utils 4.0.0

Drops `product`, `recurring_order`, `recurring_order_item`, `task_state`,
`workflow_template` and the FK columns `order.recurring_order_id`,
`order_item.product_id`, `service_plan.product_id`,
`client_service.recurring_order_id`, `task.task_state_id` (with
`uq_order_active_recurring_due_date`, `idx_order_item_product`,
`uq_service_plan_product`). Hand-written, `lock_timeout = 5s`, idempotent
(every step guarded by table/column existence), irreversible
(`downgrade()` raises `NotImplementedError`, like `ng2_topology_drop`).
Order: (a) grant-copy `products.*`->`service_plans.*`,
`recurring_orders.*`->`client_services.*` (incl. suspend/reactivate/generate)
once; (b) every ACTIVE `recurring_order` that no `client_service` bills
becomes a `client_service` (same shape as c2b Pass 2, `migration_source='ld1'`)
and its orders are repointed via `order.client_service_id` - a row without a
client, with other than one item, or without a bridged plan RAISES (nothing is
silently dropped); (c) delete workflows triggered on / referencing
`recurring_order`, `product`, `task_state` (or `task_state_id`, `product_id`,
`recurring_order_id` in step config / trigger conditions); (d) delete the
`products.%`, `recurring_orders.%`, `task_states.%`, `workflow_templates.%`
permissions; (e) drop columns + indexes; (f) drop tables and the
`taskstatecolor` enum; (g) post-asserts. Tasks/templates linked via the
`RECURRING_ORDER` enum label are nulled (the PG label stays; the Python member
is gone). `recurrenceenum` / `recurringorderstatus` stay.
`alembic/env.py` now gates the ISP seed on `client_service` instead of
`workflow_template`. **Release order:** consumers must deploy code that no
longer touches these tables before this revision reaches a database.
Guardrails: `tests/test_legacy_drop.py`.

### `ci1_category_icons` (2026-10-01)

One lucide icon mapping across seed, DB, backoffice and mobile (feature
`category-icons`). **Data-only**, with no schema or model change.
`_REMAP = {'ROUTER': ('radio-tower', 'router'), 'OLT': ('radio', 'server')}`.
`upgrade()` sets the new name only where the icon is still the old default or
NULL, so an icon customised through the API is left alone. `downgrade()`
reverts only rows still on the new name. Re-running it is a no-op.
`isp_seed.DEVICE_CATEGORIES` carries the new names for fresh databases.
`inv1._ICON_BACKFILL` is history and keeps the old names;
`tests/test_general_inventory.py` checks that inv1's backfill, after ci1's
remap, equals the seed. Default icons of the active categories: ROUTER
`router`, SWITCH `network`, OLT `server`, ONU `house-wifi`, FIBER_OPTIC and
PATCH_CORD `cable`. Verified up, re-run, down and up on a scratch Postgres
(including a NULL icon and a custom icon).

### `rt1_auth_refresh_token` (2026-09-30)

Refresh-token reuse detection (bug-fix `refresh-token-reuse`). **Additive
only**: creates `auth_refresh_token` (PK `jti` VARCHAR(64), `family_id`,
`user_id` FK `user` CASCADE, `company_id` FK `company` CASCADE nullable,
`client_type` CHECK `web`/`mobile`, `issued_at`, `expires_at`, `rotated_at`,
`replaced_by`, `revoked_at`) + indexes on `family_id`, `user_id`,
`expires_at`. No backfill: refresh tokens issued earlier have no row (the
oldest have no `jti` either) and auth-erp accepts each once, migrating it into
a new family. `downgrade()` drops the table. Verified up/down/up on a scratch
Postgres 16.

### `mp1_technician_plan_read` (2026-10-03)

Data-only, on `ld1_legacy_drop`. Grants `service_plans.read` to the global
TECHNICIAN role so the tecnicos app's install-order sheet can list plans
(`POST /tasks` with `service_plan_id` in backend-erp). Mirrored in
`isp_seed.ISP_ROLES['TECHNICIAN']`; pinned by
`tests/test_technician_plan_read.py`. `downgrade()` removes only that grant.

### `pd1_client_payment_day` (2026-10-03)

Additive, on `mp1_technician_plan_read`. `client.payment_day` SMALLINT NULL +
CHECK `ck_client_payment_day_range` (NULL or 1..31). `ClientBase`/`ClientOut`/
`ClientCreate` carry `payment_day`, `ClientUpdate` too (`ge=1, le=31`).
No backfill. `downgrade()` drops the check and the column.

### `pt1_port_topology` (2026-10-04, head) — models-utils 4.4.0

Additive and inert, on `sh1_service_history_repair`; hand-written (lock_timeout,
`IF NOT EXISTS`, existence-guarded `ADD CONSTRAINT`, post-upgrade assertions).
Doc 40 §3.1.1: `device_type.port_template`/`path_role` +
`ck_device_type_ports_serialized`; `inventory_item.uq_inventory_item_id_company`;
tables `inventory_item_port` and `network_link` with composite
(port, item, company) FKs (port FKs NO ACTION); the deferred constraint triggers
`trg_network_link_parent_sync` / `trg_inventory_item_link_sync` and their
plpgsql functions (`NETWORK_LINK_PARENT_MISMATCH` at COMMIT), Alembic-only.
No rows are created. `downgrade()` refuses while any `network_link` row or
`origin = 'ITEM'` port exists (iv1 precedent), otherwise drops everything.
Details: [network-models.md](network-models.md). Pinned by
`tests/test_port_topology.py` (static + SQLite) and `tests/pg` (CI job `pg`).
The resolver half of C1 (port attributes, role frames, resolution-time refusal,
`playbook_version` in `provisioning_run.plan`) needs **no** schema change: the
run's `path`/`plan`/`frames` are JSON. Behaviour only changes for a backend
once it pins this SHA (C2). Before composing, re-check `alembic heads` — pt1
assumes `sh1_service_history_repair` is head.

### `cr1_cash_review` (2026-10-03)

Additive, on `pd1_client_payment_day`. Admin (not the collector) closes the
cash box: `cashsessionstatus += SUBMITTED, REJECTED, APPROVED` (CLOSED /
DEPOSITED stay for legacy rows; labels added in an autocommit block and unused
in the same revision); `cash_session` gains `submitted_at`, `reviewed_at`,
`review_note` (TEXT), `reviewed_by` (FK `user` SET NULL, `CashSession.reviewer`)
and index `ix_cash_session_company_status`; new permission
`cash_sessions.review` (row in `rbac_seed.PERMISSIONS_DATA`, granted to the
global ADMIN only — no base role carries it). `CashSessionOut` gains
`submitted_at`, `reviewed_at`, `reviewed_by_name`, `review_note`.
Downgrade drops columns/index/grant; enum labels stay (PG cannot drop them).
Pinned by `tests/test_cash_review_models.py`.

### `mi2_mobile_field_ops` (2026-09-29)

Field apps on the real system (uplink-mobile cobros + tecnicos). **Additive
only**: every new column is nullable or has a server default, and
`downgrade()` drops exactly what `upgrade()` added.

- `task`: `started_at`, `completed_at` (TIMESTAMPTZ), `step_progress` (JSON
  NOT NULL default `{}`, keyed by the app's step id).
- `task_material` (new): `quantity` (>0, in `device_type.unit`),
  `consumed_quantity`, `shortfall`, `consumed_at`, `updated_by`; UNIQUE
  `uq_task_material_task_type (task_id, device_type_id)`, index
  `ix_task_material_company_task`.
- `inventory_item`: `latitude`, `longitude`, `gps_precision_m`; `warehouse`:
  `latitude`, `longitude`. The lot quantity CHECK is **unchanged** (an
  exhausted lot becomes `RETIRED`).
- `user_notification` (new; `notification` is the invitation table): `kind`
  CHECK `ck_user_notification_kind` (TASK_ASSIGNED / TASK_OVERDUE /
  PAYMENTS_OVERDUE), `entity_type`/`entity_id`, `dedupe_key` (UNIQUE per
  user), `payload`, `read_at`; feed index `ix_user_notification_feed`.
- `cash_session`: `opening_cents` (NOT NULL default 0, CHECK >= 0),
  `deposited_at`, `deposited_cents`, `deposit_reference`,
  `closed_expected_cash_cents`, `reopen_count`.
- `cash_movement` (new): top-ups; the client-supplied `id` is the
  idempotency key; `amount_cents > 0`.
- `payment`: `allocation_id`, `cash_session_id` (FK `cash_session` SET NULL);
  indexes `ix_payment_company_allocation`, `ix_payment_cash_session`,
  `ix_payment_company_received_paid (company_id, received_by, paid_at DESC)`.
- `uploaded_file`: `idempotency_key` VARCHAR(80), partial UNIQUE
  `uq_uploaded_file_company_idem`.
- `company`: `mobile_settings` JSON (`schemas.company.MobileSettings`).
- `order`: partial index `ix_order_open_receivables (company_id, due_date)
  WHERE status='ACTIVE' AND payment_status IN ('PENDING','PARTIAL')`.

Backfills: `task.completed_at = updated_at` for DONE tasks;
`payment.cash_session_id` from the collector's box whose
`[opened_at, closed_at]` window holds `paid_at`. RBAC (cfg3 pattern, mirrored
in `isp_seed.ISP_ROLES['COLLECTOR']`, pinned by
`tests/test_mobile_rbac_seed.py`): COLLECTOR gains `mobile.collector`,
`tasks.create`, `service_plans.read`, `inventory_items.read`.

Verified on PG 16 against a copy of the local DB (prod data, at
`lp1_link_ports`): `upgrade head` -> `downgrade lp1_link_ports` ->
`upgrade head`, single head; autogenerate shows no drift for any mi2 object.

### `mi1_mobile_enum_labels` (2026-09-29)

**Merge point** of `dr1_task_route_sequence` and `lp1_link_ports` (both
branches hang off `tr1_transport_axis`), and enum labels only:
`taskjobkind += RELOCATION`, `cashsessionstatus += DEPOSITED`,
`equipmenteventtype += CONSUMED, RELEASED`. Downgrade is a documented no-op
(PG cannot drop a label; tj1 precedent). The single-head file scan in
`tests/test_client_install_field_drop.py` reads tuple `down_revision`s.

### `dr1_task_route_sequence` (2026-09-28)

Adds `task.route_sequence` (INTEGER, nullable): the stop's order in its
technician's route for the task's `scheduled_date`, written by backend-erp's
`POST /dispatch/routes` (dispatch ETL) and read by the technician app. It is
not `position`, which the move and reorder endpoints renumber. No index, the
reads are covered by `ix_task_company_scheduled_date`. Additive and reversible.

### `ts1_task_status` (2026-09-28)

Fixed task status. Adds `task.status` (NOT NULL, default `PENDING`, CHECK
`ck_task_status` over PENDING/ASSIGNED/IN_PROGRESS/DONE, index
`ix_task_company_status`) and backfills it from each task's
`task_state.kind`: CANCELLED and DONE become DONE, IN_PROGRESS stays, and
ASSIGNED becomes ASSIGNED only when the task has a technician
(`task_assignee.role` TECHNICIAN or a legacy NULL), otherwise PENDING.
`task.task_state_id` becomes nullable. Nothing is dropped: the table, the FK
and the `task_states.*` permissions go in a later, destructive revision.

Installed workflows are rewritten in the same revision: a task trigger on
`task_state_id` becomes a trigger on `status` with the value mapped through
the state's kind, and `task_state_id` in a step config (top level, `data` or
`updates`) becomes `status`. A UUID that matches no task_state row is left
alone, and the engine still accepts a legacy `task_state_id`. The
`new-installation` and `installation-provisioning` seed templates drop their
board-column parameters and are gated on `task.status`.

`downgrade()` is a real inverse for the data it can map: it points every task
back at a column of the matching kind (creating Asignadas, En proceso and
Finalizadas for a company that has none), maps the workflows back and drops
the column, index and CHECK. Verified on PostgreSQL 16: upgrade, downgrade and
upgrade again.

### `tr1_transport_axis` (2026-09-26)

On `ac1_acs_tenant_auth`. Collapses the whole `network_access` table into the
tenant singleton `provisioning_settings` and replaces `mode` with two orthogonal
columns. Rationale in full in
[network-models.md](network-models.md#the-transport-axis-tr1_transport_axis-2026-09-26--and-the-network_access-table-it-replaced);
the short version is that `kind` conflated a settings bucket with a transport,
multi-row existed only for a per-CIDR `mgmt_subnets` resolver that was never
implemented and is now abandoned, and `mode` enumerated the cross product of two
independent questions — so the fifth real scenario (ZeroTier with managed routes)
had no value available.

Eight columns on `provisioning_settings`: `dial_target` (`device`|`gateway`, NOT
NULL `server_default 'device'`), `proxy_kind` (`none`|`socks5`, NOT NULL
`server_default 'none'`), `proxy_address`, `gateway_host`, `acs_base_url`,
`acs_auth_required` (BOOLEAN NOT NULL `server_default false`), and
`cwmp_credential_id` / `cwmp_pending_credential_id` (FK `device_credential.id`
ON DELETE SET NULL). Five CHECKs, fragments shared byte-for-byte with
`models/isp.py` and pinned by `tests/test_transport_axis.py`:

```
ck_provisioning_settings_dial_target    dial_target IN ('device','gateway')
ck_provisioning_settings_proxy_kind     proxy_kind IN ('none','socks5')
ck_provisioning_settings_proxy_address  proxy_kind <> 'socks5' OR proxy_address IS NOT NULL
ck_provisioning_settings_gateway_host   dial_target <> 'gateway' OR gateway_host IS NOT NULL
ck_provisioning_settings_cwmp_pair      cwmp_pending_credential_id IS NULL
                                        OR cwmp_credential_id <> cwmp_pending_credential_id
```

**A sixth CHECK was specced and deliberately NOT created**: `cwmp_pending_credential_id
IS NULL OR cwmp_credential_id IS NOT NULL` ("no pending without a current") is
violable by a DATABASE REFERENTIAL ACTION, not only by application code. Both cwmp
FKs are ON DELETE SET NULL, so deleting the current credential while a rotation
window is open nulls `cwmp_credential_id` with the pending pointer still set → CHECK
violation → an ordinary `DELETE /device-credentials/{id}` becomes a raw 500. A
company delete has the same shape (`device_credential` and `provisioning_settings`
both CASCADE from `company`, and Postgres does not order the SET NULL against the
CASCADE). The invariant belongs in backend-erp's router as a 409, which also closes
the pre-existing gap that a plain DELETE of an ACS Inform credential was never
rollout-gated.

**Order inside `upgrade()` is load-bearing:**

1. the eight columns and both FKs (they target `device_credential`, never
   `network_access`, so they are order-independent w.r.t. step 5);
2. assert no `mode='tunnel'` row exists — it has no mapping on the axis and the
   API never allowed it, so the revision aborts by name rather than guessing — and
   COUNT AND LOG the non-default rows that are about to die with the table;
3. the fold, `INSERT ... ON CONFLICT (company_id) DO UPDATE`, per company that has
   any `network_access` row: `direct`→device+none, `vpn`→device+socks5 carrying
   `vpn_socks5`, `nat_public`→gateway+none, `nat_zt`→gateway+socks5 carrying
   `pylon_socks5`, `gateway_host` verbatim, and `acs_base_url`/`acs_auth_required`
   off the default `kind='acs'` row;
4. the cwmp pair, from `device_credential.network_access_id` — **before** step 5,
   because that column is the ONLY thing identifying which credentials were the
   tenant's ACS Inform pair (newest `HTTP_BASIC` bound to the default `acs` row →
   current, second-newest → pending, `created_at DESC, id DESC`);
5. `DROP COLUMN device_credential.network_access_id`, then `DROP TABLE
   network_access` (that FK was the only thing pointing at it);
6. the CHECKs LAST, so a pre-existing inconsistency surfaces as a named
   constraint violation on real data rather than aborting a DDL step mid-fold.
   They are satisfiable by construction: the old
   `ck_network_access_nat_gateway_host` / `_pylon_socks5` / `_vpn_socks5`
   guarantee each operand is non-NULL wherever the folded axis requires it.

**The INSERT branch writes `enabled = false`, and that is the only branch that
runs on Railway development** (`provisioning_settings` had zero rows there while
the one tenant had two `network_access` rows). `ProvisioningSettings.enabled` is a
live provisioning gate and canon C6 says absence of the row means DISABLED, so
`enabled = true` would silently turn provisioning ON for a real tenant with no
operator action. `default_inform_interval` stays NULL for the same reason. Pinned
by `tests/test_transport_axis.py::test_tr1_inserts_provisioning_disabled`.

`downgrade()` recreates the table with every column, CHECK, index and the
`device_credential.network_access_id` FK, and moves the values back into one
`outbound` + one `acs` row per tenant (device+none→`direct`,
device+socks5→`vpn`, gateway+none→`nat_public`, gateway+socks5→`nat_zt`), then
re-binds the two cwmp credentials to the recreated `acs` row. It is reversible in
substance but **NOT a true inverse**, and the docstring says so rather than
claiming otherwise: `network_access.name` is NOT NULL and UNIQUE per company with
no destination column, so the names `'ACS'`/`'Outbound'` are synthesised and will
collide if a tenant separately holds a row of that name; `mgmt_subnets` is gone for
good; a tenant that had a settings row but never a `network_access` row is
indistinguishable after the fold and gets rows too; and any NON-cwmp
`network_access_id` binding (a company-default SSH/WIREGUARD credential pinned to
the outbound row) comes back unbound — which is still the company default under the
new resolution order.

Verified on PG 16 on a scratch database `tr1_scratch` (created and dropped; the
shared local dev DB was not touched): `upgrade head` → `downgrade
ac1_acs_tenant_auth` → `upgrade head`, with three seeded tenants proving every
branch — one mirroring Railway development exactly (default `acs` row + default
`outbound` `vpn` row + two `HTTP_BASIC` credentials), one `nat_zt` with an armed
`acs` row and a stray non-default row, and one that already had a
`provisioning_settings` row (`enabled=true`, interval 300) to exercise the ON
CONFLICT branch and prove those two values survive both directions. The
`mode='tunnel'` abort was exercised too. Guardrails:
`tests/test_transport_axis.py`, `tests/test_transport_resolver.py`.

### `lp1_link_ports` (2026-09-28)

On `tr1_transport_axis`. Additive: `inventory_item.parent_port` and
`inventory_item.uplink_port` (both `VARCHAR(64)` NULL, free text) label the
`parent_id` edge of the network graph, plus the partial unique index
`uq_inventory_item_parent_port` on `(parent_id, parent_port)` WHERE
`parent_port IS NOT NULL` (one parent port feeds one child). `downgrade()` drops
the index and both columns — loses only the port labels. See
[network-models.md](network-models.md#link-ports-lp1_link_ports).

## Four built-in roles (rr1_four_builtin_roles)

On `ci1_category_icons`. Product decision 2026-10-02: the global roles collapse
to **ADMIN** (wildcard), **VIEWER** (every `read` permission + `web.access`),
**COLLECTOR** and **TECHNICIAN** (mobile-only, grants unchanged). Tenant custom
roles are untouched. The revision:

- inserts the `web.access` permission (gates the web dashboard in
  frontend-erp) and grants it to VIEWER and to **every existing tenant custom
  role** (nobody loses the web app; tenants untick it for mobile-only roles);
- creates VIEWER;
- renames tenant custom roles whose name collides case-insensitively with a
  built-in to `"<name> (custom)"`;
- remaps holders in `user_role`, `user_invitation_role` and
  `notification.pending_role_ids`: MANAGER→ADMIN, BILLING→COLLECTOR,
  SALES/USER/NOC/WAREHOUSE/SUPPORT→VIEWER (deduplicated), then deletes those
  seven global roles.

`rbac_seed` / `isp_seed` were edited in the same commit to stop re-creating the
removed roles (they run after every alembic command); `_ensure_convergent_rbac`
step 3 now grants VIEWER every `read` permission + `web.access`
(`rbac_seed.VIEWER_PERMISSION_FILTER`), so future read permissions converge.
**Downgrade is lossy**: it restores the seven role rows (USER with its original
read grants, the rest empty), moves VIEWER holders to USER and drops
`web.access`, but cannot restore who held MANAGER/SALES/NOC/... Pinned by
`tests/test_four_builtin_roles.py`.

## Service history repair (sh1_service_history_repair)

On `cr1_cash_review`. Data-only, one-way (`downgrade()` raises). Reviewed by the
product owner (2026-10-04) after an investigation of 184 RECURRING orders whose
single line item named the client's *current* plan while `order.total_cents` and
the payments held the price actually charged (adoption import attached each
client to one service; `c1b_backfill` priced lines from the current product).

- Tidy first: services `ACTIVE` + billing `INACTIVE` replaced by a later
  non-cancelled service of the same client (no order overlap) become
  `CANCELLED` at the replacement's start; `recurrence_end` = their last billed
  order, `next_generation_date` NULL (lifecycle cancel side effects).
- Clean switch (older price run(s) then only the current price): one CANCELLED
  historical service per run on the company's unique SERVICE-kind plan at that
  price (installation plans excluded); orders + line items move to it;
  `recurrence_end` = the run's last order so `detect_missing_periods` expects
  exactly what it billed; the current service's `activation_date` **and**
  `created_at` move to its first current-price order (gap detection anchors on
  `created_at`).
- Anything else (one-off odd month, first-month discount, unmatched price,
  multi-line orders): line price := order total / quantity only.
- Verified on a prod snapshot copy: 39 tidied (1 overlap skipped), 51 historical
  services, 163 orders moved, 184 lines fixed, 49 start dates shifted, 0 services
  with new billing gaps; re-running the logic is a no-op.

## Key rules

- **Not all migrations are reversible**: `c1e_install_actions` uses
  `ALTER TYPE ... ADD VALUE`, which has no downgrade (so do `pm1`, `tj1` and
  `iv1_insights_v2`, which keep their labels on downgrade), and `ng2_topology_drop`
  raises from `downgrade()` by design. `tr1_transport_axis`'s `downgrade()` runs
  and restores every value, but is not a true inverse (synthesised
  `network_access.name`, `mgmt_subnets` unrecoverable — see its section above).
  `nat1_gateway_transport`'s `downgrade()` was conditionally reversible on a
  `network_access` row still being in a NAT mode; that table no longer exists
  past `tr1`, so the condition is vacuous. Check each revision's `downgrade()`
  before assuming rollback is possible
- Additive changes (new columns/tables): safe to apply before consuming
  service code ships
- Destructive changes (removing/renaming): apply AFTER all consuming service
  code is in production
- Parallel schema features use separate branches/revisions — never combine
  unrelated schema changes

## Environment Variables

- `DATABASE_URL` / `DB_URL` / `POSTGRES_USER`+`POSTGRES_PASSWORD`+`POSTGRES_HOST`+`POSTGRES_PORT`+`POSTGRES_DB` — connection for `alembic/env.py`

### `cc1_client_code` (2026-10-05)

Additive, on `pt1_port_topology`. `client.code` VARCHAR(16) NOT NULL: a short
per-company client id. Backfill: `[LEGACY_ID:<code>]` in `observations` (uppercased)
when well-formed and unique within the company; every other client gets a random
6-char code. DB default `client_code_generate()` (plpgsql, alphabet without
0/O/1/I/L, hand-synced with `utils/client_code.py`) so writers that predate the
column still insert a code. `ck_client_code_format` (`^[A-Z0-9-]{1,16}$`,
Alembic-only — PG regex) + `uq_client_company_code` on `(company_id, upper(code))`.
`downgrade()` drops the index, CHECK, column and function.


### `vw1_viewer_no_credential_read` (2026-10-05)

Data-only, on `cc1_client_code`. Deletes the global VIEWER role's
`device_credentials.read` grant (rr1 gave VIEWER every `*.read`); founder
decision D2 of the v1.0.0 release plan. `rbac_seed.VIEWER_PERMISSION_FILTER`
excludes it in the same commit, otherwise the post-upgrade seed would re-grant
it. Downgrade re-grants it.


### `pt2_unmap_port_labels` (2026-10-06)

No-op, on `vw1_viewer_no_credential_read` (doc 40 §4.2 C8a). The model stops
mapping `inventory_item.parent_port` / `uplink_port` and
`uq_inventory_item_parent_port`; the DB keeps all three so a backend still on
the old models keeps working during the rollout. The revision exists for the CI
migration guard. The drop is `pt3_drop_port_labels` (C8b), which ships only
after every backend deployed against the database runs C8a (doc 40 DI-13).
