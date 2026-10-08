# models-utils

Shared Python library (package `database-utils`, module `database_utils`) — the
**schema authority** of the Uplink platform. Contains all SQLAlchemy models,
Pydantic schemas, Alembic migrations + seeds, cross-service utilities (JWT,
permissions, audit, SSRF guard), the **workflow engine**, provisioning
resolution, and the transactional email service. Not a running service — no
server, no port.

**This is a critical dependency — changes affect `backend-erp`, `auth-erp`, and `cron-erp`.**

Published to GitHub, consumed pinned by commit SHA:
`database-utils @ git+https://github.com/InnovaTech-GT/models-utils.git@<sha>`

Docs wiki: [docs/README.md](docs/README.md) · Navigation: [CODEBASE_INDEX.md](CODEBASE_INDEX.md)

## Commands

```bash
pip install -e . -r requirements-dev.txt          # editable local dev + test deps (pytest-asyncio required)
pytest -v                                         # 737 tests, in-memory SQLite (needs placeholder POSTGRES_* env); the 18 tests/pg skip unless PG_TEST_URL
alembic revision --autogenerate -m "description"  # generate revision (needs reachable DB env)
```

## Git Policy

Same feature branch + PR model as all other services. **Never push directly to `main` or `develop`.**

Branch naming: `{type}/{feature-id}/models-{description}` (e.g. `feat/tier-billing/models-subscription`)

Database migrations:
- **Local**: the docker compose `migrate` service (built from this repo's `Dockerfile`) runs `alembic upgrade head` + seeds on every `docker compose up`
- **Railway development / production**: GitHub Actions (`.github/workflows/migrate.yml`) runs `alembic upgrade head` on push to `develop` (GitHub env `development`) or `main` (GitHub env `production`), each env's own `DB_URL` secret, `alembic/**` path filter

## Migration Workflow (Alembic)

Use the **erp-migration** skill — it covers the full ordered cycle automatically.

Correct order:
1. Create the feature branch from an up-to-date `origin/main` for a standalone feature, or from `origin/develop` when it builds on cycles already on `develop` but not yet released
2. Edit model + schema + bump `version` in `setup.cfg`
3. Generate revision: `alembic revision --autogenerate -m "description"`
4. Commit and push feature branch
5. Compose into `develop` (erp-release) and push — the "Alembic Migrate" workflow applies the revision to the Railway `development` Postgres; **wait for it to succeed before pushing the backends**. Locally, the `migrate` compose service applies it on `docker compose up`
6. Pin consuming services (`backend-erp`, `auth-erp`) to the branch commit SHA in their `requirements.txt`
7. After E2E passes on Railway development, merge the `develop` → `main` release PR — GitHub Actions migrates the prod DB

## Key Directories

- `database_utils/models/` — SQLAlchemy models (UUID PKs, created_at/updated_at), registered in `__init__.py` for Alembic autogenerate
  - `auth.py`: Tier, Company, User, Role, Permission, Notification, AuditLog, UserInvitation, EmailVerificationToken, PasswordResetToken, RefreshToken (rt1 — `auth_refresh_token`, server-side refresh-token store for rotation reuse detection), Subscription, PaymentMethod, BillingInvoice, BillingWebhookEvent (rb1 — Recurrente webhook delivery idempotency log; Tier/Company/Subscription/BillingInvoice also carry `recurrente_*` gateway columns). `TierChangeRequest` was removed (superseded by Recurrente self-serve checkout/cancel) — its `tier_change_request` table is still physically present pending a later destructive-change release, see [docs/limitations.md](docs/limitations.md)
  - `crm.py`: Client (installation_status/installation_date + `InstallationStatus` enum dropped by `cf1` — install truth is `client_service.install_state`; `ClientOut` carries backend-computed `services_total`/`services_installed` rollups), Order, OrderItem, Invoice, Payment (Cycle 1 ledger), custom fields, Task/TaskTemplate, Integration
  - `isp.py` (largest): ServicePlan, ClientService, ServiceSuspension, DeviceCategory, DeviceType, Warehouse, InventoryItem, EquipmentEvent, Playbook, **DeviceTypePlaybook**, **InventoryItemPlaybook**, **ProvisioningRun**, ProvisioningJob; Cycle-4 insights (v2 since iv1_insights_v2: `InsightChartType.LINE`, opaque JSON `insight_dashboard.default_time_range` / `insight_chart.viz`; `spec` is opaque query-spec v2 owned by backend-erp), Cycle-5 network-config tables, Cycle-7 core-config columns (`device_category.tier`, `device_type.cli_platform`, inventory mgmt surface, `client_service.install_state`/`installed_at`); Cycle-8 (`playbook` drops `target_vendor`/`target_category_id`); **Cycle-10 network graph (doc 35, ng1/ng2): `Topology`/`TopologyDeviceType`/`TopologyPlaybook` are DELETED** — the plant is a tree of inventory items (`inventory_item.parent_id` self-FK RESTRICT + `network_attached`, guarded by the `trg_inventory_item_graph_guard`/`trg_inventory_item_detach_guard` triggers), `device_category.is_passive`, playbooks bind per device type with a per-node override, `client_service.cpe_item_id`/`path_changed_at` replace `topology_id`, `service_plan.default_topology_id` and `playbook.topology_id` are dropped, and `provisioning_job.run_id`/`run_position` hang children off a `ProvisioningRun`; purpose constants renamed `CANONICAL_PLAYBOOK_PURPOSES` / `PLAYBOOK_PURPOSE_PATTERN`; brownfield adoption attestation columns on ClientService (`adopted_at`/`adopted_by_user_id`/`adoption_note`, revision ba1); per-service provisioning parameters (revision sp1: `client_service.provisioning_params`, plus an optional `scope` on `service_plan.provisioning_params` rows); **The transport axis (2026-09-27, revision `tr1_transport_axis`)**: `NetworkAccess` and the whole `network_access` table are **DELETED** and folded into `ProvisioningSettings`, which gains `dial_target` (`device`|`gateway`, NOT NULL default `device`), `proxy_kind` (`none`|`socks5`, NOT NULL default `none`), `proxy_address`, `gateway_host`, `acs_base_url` (read-only), `acs_auth_required` (the Capa 3 gate, moved) and `cwmp_credential_id`/`cwmp_pending_credential_id` (FK `device_credential` SET NULL, the rotation window made explicit) + five CHECKs. Constants `DIAL_TARGETS`/`PROXY_KINDS` replace `NETWORK_ACCESS_KINDS`/`NETWORK_ACCESS_MODES`/`NAT_MODES`; `DeviceCredential.network_access_id` is gone and its binding tier becomes "both FKs NULL = the company default". `mgmt_subnets` and per-CIDR longest-prefix resolution are **dead, not deferred**. Still live from `nat1`: `InventoryItem.nat_port` (Integer, nullable, range-CHECKed 1–65535, unique per `company_id` where set) and `InventoryItem.mgmt_host_key` (String, nullable — pinned SSH host key, TOFU, first successful connect), plus `mgmt_port`'s range CHECK; **port-level topology (doc 40, revision `pt1_port_topology`)**: `InventoryItemPort` (`inventory_item_port`, generated from `device_type.port_template` or added per item) and `NetworkLink` (`network_link`, one upstream link per device, composite (port, item, company) FKs; `parent_id` stays the traversal source and a link always agrees with it — deferred triggers at COMMIT), `device_type.port_template`/`path_role`; the lp1 labels `inventory_item.parent_port`/`uplink_port` are **unmapped since `pt2_unmap_port_labels`** (5.0.0, C8a — the columns stay until `pt3_drop_port_labels`/C8b); `provisioning_job.claim_token` (`pc1`, 5.1.0) is the worker's per-claim fence token. See [docs/network-models.md](docs/network-models.md) and [docs/utilities.md](docs/utilities.md)
  - `workflow.py`: Workflow, WorkflowTrigger, WorkflowStep, WorkflowStepEdge, WorkflowExecution, WorkflowStepExecution
- `database_utils/schemas/` — 37 Pydantic v2 modules; `__init__.py` star-imports all + `model_rebuild()`. `topology.py` was deleted in Cycle 10; its `normalize_purpose` now lives in `playbook.py`
- `database_utils/utils/` — 28 modules; notable: `playbook_expr.py` (doc 40 §3.3.3: declared integer arithmetic for playbook `computed` blocks — hand-written tokenizer + recursive descent, no eval; `is_secret_name` lives here now), `workflow_engine.py` (trigger matching + DAG execution; `ENQUEUE_PROVISIONING` mode A key is **`use_service_path`** — `use_topology` raises — and opens a `ProvisioningRun`), `network_graph.py` (the only walker of the plant tree: `resolve_path` leaf→root, `descendants`, `would_create_cycle`, `child_count`, `MAX_PATH_DEPTH=32`, company-scoped recursive CTEs; `assert_links_consistent` is the SQLite tests' stand-in for pt1's link triggers), `provisioning_resolution.py` (CPE → walk to root → per-node playbook via node override → device-type default → none; namespaces **`device.*` / `cpe.*` / `path.<category_key>.*`** plus `service_plan`/`client`/`service`/`input` — `chain[n]`/`edge_devices[n]`/`core_devices[n]` are retired with no shim; doc 40 adds `path.<path_role>.*` (roles held once; `ROLE_SHADOWS_CATEGORY`), `PORT_ATTRIBUTES` `out_slot`/`out_port`/`out_port_name` (emitted only when known, from the link below and only if it names this node), `computed.*`, and resolution-time refusal — `ROLE_AMBIGUOUS` / `PORT_NOT_RECORDED` / `UNRESOLVED_TOKEN` / `COMPUTE_*` for every purpose, `PATH_CHANGED_SINCE_ACTIVATION` for the others — collected into one `RESOLUTION_FAILED`), `provisioning_runs.py` (`create_run`/`advance_run` — lazy one-child-at-a-time execution; plan entries carry `playbook_version`; children are inserted with `device_lock_key` NULL — **producers never lock**, only backend-erp's worker claim does; `advance_run` locks the run row and no-ops on a terminal run / stale or duplicate advance; `create_or_get_run` = savepoint-deduped create; `repair_stranded_runs` = reaper backstop; `run_idempotency_key` is the single shared key `deprovision-{id}` / `path-provision-{id}-{purpose}[-dry]`), **`transport.py`** (2026-08-13, doc 34 R23 rewrite; transport axis 2026-09-27: `resolve_endpoint(db, item, company_id, default_port, settings=None)` — the one place `InventoryItem` + the tenant's `ProvisioningSettings` row become a dial target, returning `ResolvedEndpoint(host, port, proxy, dial_target)`. `host = gateway_host if dial_target=='gateway' else item.mgmt_host`, `port = item.nat_port if gateway else (mgmt_port or default_port)`, `proxy = proxy_address if proxy_kind=='socks5' else None`. Fails closed: `NAT_MAPPING_NOT_SET` / `MGMT_HOST_NOT_SET` / `PROXY_NOT_PROVISIONED` (one code — the hub technology is not the resolver's business) / `TRANSPORT_UNAVAILABLE` (a caller-supplied row from another company). `company_provisioning_settings()` replaces `default_outbound_access()` — see docs/utilities.md), `jwt_utils.py` (access tokens from a pair carry `sid` = refresh family), `sessions.py` (access-token revocation: 401 `SESSION_REVOKED`, ≤ 30 s cross-process latency, legacy no-`sid` tokens accepted until expiry), `permission_utils.py`, `audit_utils.py`, `ssrf.py`, `crypto.py`, `tier_limits.py`, `timezone_utils.py` (America/Guatemala)
- `database_utils/dependencies/` — `get_db` FastAPI session dependency, audit context
- `database_utils/middleware/` — request-ID/JWT-context logging ASGI middleware
- `database_utils/services/email_service.py` + `database_utils/templates/email/` — transactional email (SMTP via aiosmtplib) + 13 Jinja2 templates (shipped via `[options.package_data]`)
- `database_utils/database.py` — engine bootstrap from `DATABASE_URL`/`DB_URL`/`POSTGRES_*` (raises at import if none)
- `alembic/` — 101 revisions (head: `ri1_inventory_received_at` (`inventory_item.received_at` NOT NULL, backfilled from `created_at` — doc 47 FIFO key) ← `ta1_task_assignee_model` (schema check only for the `TaskAssignee` model) ← `pc1_provisioning_claim_token` (provisioning claim fence token, nullable, metadata-only) ← `pt2_unmap_port_labels` (InventoryItem stops mapping the lp1 port labels and their unique index is dropped, doc 40 C8a) ← `vw1_viewer_no_credential_read` (VIEWER loses device_credentials.read) ← `cc1_client_code` (client short codes) ← `pt1_port_topology` (doc 40: ports, network links, deferred link⇔parent_id triggers) ← `sh1_service_history_repair` (one-way data repair) ← `cr1_cash_review` ← `pd1_client_payment_day` ← `mp1_technician_plan_read` ← `ld1_legacy_drop` (destructive: drops product/recurring_order/task_state/workflow_template), on `rr1_four_builtin_roles`, on `ci1_category_icons`, on `rt1_auth_refresh_token`, on `mi2_mobile_field_ops`, on `mi1_mobile_enum_labels` — the merge of `dr1_task_route_sequence` and `lp1_link_ports` — on `ts1_task_status` ← `tr1_transport_axis` ← `ac1_acs_tenant_auth` ← `na1_kind_outbound` ← `vpn1_vpn_socks5` ← `iv1_insights_v2`, on `dc1_category_trim` ← `fg1_integration_enabled_regby` ← `ng2_provisioning_run_list` ← `nat3_pylon_socks5` ← `nat2_gateway_host_check` ← `nat1_gateway_transport` ← `ng2_topology_drop` ← `ng1_network_graph` ← `lc2_retire_susp_react`); `env.py` imports all model modules and runs seeds after upgrade
- `alembic/seeds/` — idempotent seed scripts: `rbac_seed.py`, `tier_seed.py`, `isp_seed.py` (importable as `seeds.*` because `env.py` adds the alembic dir to `sys.path`). `isp_seed.DEVICE_CATEGORIES` rows are 7-wide `(key, name, sort_order, tier, is_passive, icon, is_active)` since `dc1_category_trim`; passive = SPLITTER / SPLICE_CLOSURE / MUFA / PATCH_PANEL / ANTENNA (UPS and RADIO deliberately stay configurable). The nine `inv1` keys are skipped at pre-`inv1` migration positions (the old tier CHECK forbids CONSUMABLE/TOOL/OTHER). Only 6 of the 22 keys are `is_active` on a fresh insert — ROUTER/SWITCH/OLT/ONU/FIBER_OPTIC/PATCH_CORD, the ones backend-erp seeds as every tenant's default products
- `tests/` — 64 files, 755 tests: 737 SQLite (`asyncio_mode = auto`) + 18 in `tests/pg` (Postgres-only, `-m pg` with `PG_TEST_URL`, run by the CI `pg` job); `conftest.py` holds the shared `db` + `plant` fixtures (a real in-memory graph, not fakes)
- `.github/workflows/` — `ci.yml` (migration guard + ruff advisory + pytest; job `pg`: postgres:16 + `alembic upgrade head` + `pytest -m pg tests/pg`), `migrate.yml` (`alembic upgrade head` on push to `develop` → Railway dev DB, `main` → prod DB)
- `Dockerfile` — exists solely for the compose `migrate` one-shot; production images never build it

## Conventions & Gotchas

- **Import direction is strictly downward**: backends import models-utils, never the reverse. Any logic the workflow engine needs (e.g. provisioning resolution) must live HERE, not in backend-erp.
- Every model change ships with an Alembic revision — CI blocks PRs to develop/main otherwise (migration guard).
- **Every seed change ships with a (possibly no-op) revision** — the prod `migrate.yml` workflow is path-filtered on `alembic/**`.
- Seeds must stay idempotent (ON CONFLICT / upsert) so SaaS-admin edits converge.
- Not all migrations are reversible: `c1e_install_actions` uses `ALTER TYPE ... ADD VALUE` (no downgrade), and `iv1_insights_v2` keeps its `LINE` label on downgrade (its `downgrade()` refuses while a chart still uses `LINE`), and `ng2_topology_drop`'s `downgrade()` raises `NotImplementedError` on purpose. `tr1_transport_axis`'s `downgrade()` runs and restores every value but is NOT a true inverse — `network_access.name` is synthesised and `mgmt_subnets` is unrecoverable.
- **DB triggers live only in their Alembic revision, never in SQLAlchemy metadata** — the test suites of every consuming service build schemas with SQLite `create_all`, which cannot parse plpgsql or the PG regex operator `~`. This covers `trg_inventory_item_graph_guard`, `trg_inventory_item_detach_guard` (ng1), the two deferred link triggers `trg_network_link_parent_sync`/`trg_inventory_item_link_sync` (pt1), `nc1b_device_audit_trigger`, and the two purpose-format CHECKs on the binding tables.
- **`network_graph.MAX_PATH_DEPTH` (32) is hand-kept in sync with the same constant inside `trg_inventory_item_graph_guard`.** If they diverge the trigger wins and traversal starts raising `PATH_TOO_DEEP` on paths the DB accepted.
- **The two token regexes in `provisioning_resolution.py` FAIL OPEN.** Adding a variable namespace without adding it to `_DEVICE_VARIABLE_PATTERN` silently downgrades a fatal resolution error into a half-configured customer. Change them in the same commit. Since doc 40 both helpers also read a playbook's `computed` operands, and an unparseable `computed` block counts as referencing everything.
- Bump `version` in `setup.cfg` for non-trivial changes (NOT `pyproject.toml` — that file only holds build-system config).
- Naming mismatch is historical and intentional: repo `models-utils`, pip package `database-utils`, module `database_utils`.
- Additive schema changes: safe once consuming code is ready. Destructive (drop/rename): all consuming service code must be in production FIRST.
- Test changes from consuming projects (`backend-erp`, `auth-erp`) before publishing.
- The legacy `Product`/`RecurringOrder`/`TaskState`/`WorkflowTemplate` models are gone (`ld1_legacy_drop`, 4.0.0, irreversible); `alembic/env.py` gates the ISP seed on the `client_service` table.

## Recommended Agents and MCP Tools

- **Model/schema implementation**: `python-pro` subagent
- **Library API lookup**: Context7 MCP (`resolve-library-id` → `query-docs`) for SQLAlchemy, Pydantic, and Alembic APIs
- **DB query/performance issues**: `postgres-pro` subagent
- **Python syntax errors** are automatically caught by hooks after every edit
