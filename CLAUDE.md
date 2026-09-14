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
pip install -e .                                  # editable local dev
pytest -v                                         # 252 tests, in-memory SQLite (needs placeholder POSTGRES_* env)
alembic revision --autogenerate -m "description"  # generate revision (needs reachable DB env)
```

## Git Policy

Same feature branch + PR model as all other services. **Never push directly to `main` or `develop`.**

Branch naming: `{type}/{feature-id}/models-{description}` (e.g. `feat/tier-billing/models-subscription`)

Database migrations:
- **Development** = the LOCAL docker compose DB — the `migrate` compose service (built from this repo's `Dockerfile`) runs `alembic upgrade head` + seeds on every `docker compose up` (Railway dev was decommissioned)
- **Production**: GitHub Actions (`.github/workflows/migrate.yml`) runs `alembic upgrade head` against prod `DB_URL` on push to `main` (alembic path filter)

## Migration Workflow (Alembic)

Use the **erp-migration** skill — it covers the full ordered cycle automatically.

Correct order:
1. Create feature branch from `main`
2. Edit model + schema + bump `version` in `setup.cfg`
3. Generate revision: `alembic revision --autogenerate -m "description"`
4. Commit and push feature branch
5. Compose into `develop` (erp-release); the local `migrate` compose service applies the revision on `docker compose up`
6. Pin consuming services (`backend-erp`, `auth-erp`) to the branch commit SHA in their `requirements.txt`
7. After E2E passes locally, merge PR to `main` — GitHub Actions migrates the prod DB

## Key Directories

- `database_utils/models/` — SQLAlchemy models (UUID PKs, created_at/updated_at), registered in `__init__.py` for Alembic autogenerate
  - `auth.py`: Tier, Company, User, Role, Permission, Notification, AuditLog, UserInvitation, EmailVerificationToken, PasswordResetToken, Subscription, PaymentMethod, BillingInvoice, BillingWebhookEvent (rb1 — Recurrente webhook delivery idempotency log; Tier/Company/Subscription/BillingInvoice also carry `recurrente_*` gateway columns). `TierChangeRequest` was removed (superseded by Recurrente self-serve checkout/cancel) — its `tier_change_request` table is still physically present pending a later destructive-change release, see [docs/limitations.md](docs/limitations.md)
  - `crm.py`: Client (installation_status/installation_date + `InstallationStatus` enum dropped by `cf1` — install truth is `client_service.install_state`; `ClientOut` carries backend-computed `services_total`/`services_installed` rollups), Product (legacy, absorbed by catalog merge), Order, OrderItem, RecurringOrder(+Item) (legacy billing, dual-written; consumed by cron-erp), Invoice, Payment (Cycle 1 ledger), custom fields, TaskState/Task/TaskTemplate, Integration
  - `isp.py` (largest): ServicePlan, ClientService, ServiceSuspension, DeviceCategory, DeviceType, Warehouse, InventoryItem, EquipmentEvent, Playbook, **DeviceTypePlaybook**, **InventoryItemPlaybook**, **ProvisioningRun**, ProvisioningJob; Cycle-4 insights, Cycle-5 network-config tables, Cycle-7 core-config columns (`device_category.tier`, `device_type.cli_platform`, inventory mgmt surface, `client_service.install_state`/`installed_at`); Cycle-8 (`playbook` drops `target_vendor`/`target_category_id`); **Cycle-10 network graph (doc 35, ng1/ng2): `Topology`/`TopologyDeviceType`/`TopologyPlaybook` are DELETED** — the plant is a tree of inventory items (`inventory_item.parent_id` self-FK RESTRICT + `network_attached`, guarded by the `trg_inventory_item_graph_guard`/`trg_inventory_item_detach_guard` triggers), `device_category.is_passive`, playbooks bind per device type with a per-node override, `client_service.cpe_item_id`/`path_changed_at` replace `topology_id`, `service_plan.default_topology_id` and `playbook.topology_id` are dropped, and `provisioning_job.run_id`/`run_position` hang children off a `ProvisioningRun`; purpose constants renamed `CANONICAL_PLAYBOOK_PURPOSES` / `PLAYBOOK_PURPOSE_PATTERN`; brownfield adoption attestation columns on ClientService (`adopted_at`/`adopted_by_user_id`/`adoption_note`, revision ba1); per-service provisioning parameters (revision sp1: `client_service.provisioning_params`, plus an optional `scope` on `service_plan.provisioning_params` rows); **NAT transport (2026-08-13, revision `nat1_gateway_transport`)**: `NETWORK_ACCESS_MODES` grows to `("direct","vpn","tunnel","nat_zt","nat_public")`, `NAT_MODES = ("nat_zt","nat_public")`; `NetworkAccess.gateway_host` (String, nullable — the tenant's own address on the path we dial) and `NetworkAccess.pylon_socks5` (String, nullable, revision `nat3_pylon_socks5` — the tenant's own Pylon SOCKS5 listener, `"host:port"`, CHECK-required when `mode='nat_zt'`); `InventoryItem.nat_port` (Integer, nullable, range-CHECKed 1–65535, unique per `company_id` where set) and `InventoryItem.mgmt_host_key` (String, nullable — pinned SSH host key, TOFU, first successful connect); `mgmt_port` also gains the range CHECK it lacked since nc2a. Purely additive, downgrade() refuses if any row is in a NAT mode — see [docs/network-models.md](docs/network-models.md) and [docs/utilities.md](docs/utilities.md)
  - `workflow.py`: WorkflowTemplate, Workflow, WorkflowTrigger, WorkflowStep, WorkflowStepEdge, WorkflowExecution, WorkflowStepExecution
- `database_utils/schemas/` — 41 Pydantic v2 modules; `__init__.py` star-imports all + `model_rebuild()`. `topology.py` was deleted in Cycle 10; its `normalize_purpose` now lives in `playbook.py`
- `database_utils/utils/` — 25 modules; notable: `workflow_engine.py` (trigger matching + DAG execution; `ENQUEUE_PROVISIONING` mode A key is **`use_service_path`** — `use_topology` raises — and opens a `ProvisioningRun`), `network_graph.py` (the only walker of the plant tree: `resolve_path` leaf→root, `descendants`, `would_create_cycle`, `child_count`, `MAX_PATH_DEPTH=32`, company-scoped recursive CTEs), `provisioning_resolution.py` (CPE → walk to root → per-node playbook via node override → device-type default → none; namespaces **`device.*` / `cpe.*` / `path.<category_key>.*`** plus `service_plan`/`client`/`service`/`input` — `chain[n]`/`edge_devices[n]`/`core_devices[n]` are retired with no shim), `provisioning_runs.py` (`create_run`/`advance_run` — lazy one-child-at-a-time execution), **`transport.py`** (2026-08-13, doc 34 R23 rewrite: `resolve_endpoint()` — the one place `InventoryItem` + the tenant's default `NetworkAccess` row become a dial target; `direct` unchanged, `nat_zt`/`nat_public` target `(access.gateway_host, item.nat_port)` never `item.mgmt_host`; `nat_zt` reads `access.pylon_socks5` (2026-08-17: per-tenant column, not a function argument — the parameter was removed to close a cross-tenant leak a stray test-fixture value could reopen) and fails closed with `PYLON_NOT_PROVISIONED` if unset — see docs/utilities.md), `jwt_utils.py`, `permission_utils.py`, `audit_utils.py`, `ssrf.py`, `crypto.py`, `tier_limits.py`, `timezone_utils.py` (America/Guatemala)
- `database_utils/dependencies/` — `get_db` FastAPI session dependency, audit context
- `database_utils/middleware/` — request-ID/JWT-context logging ASGI middleware
- `database_utils/services/email_service.py` + `database_utils/templates/email/` — transactional email (SMTP via aiosmtplib) + 8 Jinja2 templates (shipped via `[options.package_data]`)
- `database_utils/database.py` — engine bootstrap from `DATABASE_URL`/`DB_URL`/`POSTGRES_*` (raises at import if none)
- `alembic/` — 58 revisions (head: `nat3_pylon_socks5`, on `nat2_gateway_host_check` ← `nat1_gateway_transport` ← `ng2_topology_drop` ← `ng1_network_graph` ← `lc2_retire_susp_react`); `env.py` imports all model modules and runs seeds after upgrade
- `alembic/seeds/` — idempotent seed scripts: `rbac_seed.py`, `tier_seed.py`, `isp_seed.py` (importable as `seeds.*` because `env.py` adds the alembic dir to `sys.path`). `isp_seed.DEVICE_CATEGORIES` rows are 5-wide `(key, name, sort_order, tier, is_passive)`; passive = SPLITTER / SPLICE_CLOSURE / PATCH_PANEL / ANTENNA (UPS and RADIO deliberately stay configurable)
- `tests/` — 28 files, 252 tests (`asyncio_mode = auto`, SQLite); `conftest.py` holds the shared `db` + `plant` fixtures (a real in-memory graph, not fakes)
- `.github/workflows/` — `ci.yml` (migration guard + ruff advisory + pytest), `migrate.yml` (prod migration)
- `Dockerfile` — exists solely for the compose `migrate` one-shot; production images never build it

## Conventions & Gotchas

- **Import direction is strictly downward**: backends import models-utils, never the reverse. Any logic the workflow engine needs (e.g. provisioning resolution) must live HERE, not in backend-erp.
- Every model change ships with an Alembic revision — CI blocks PRs to develop/main otherwise (migration guard).
- **Every seed change ships with a (possibly no-op) revision** — the prod `migrate.yml` workflow is path-filtered on `alembic/**`.
- Seeds must stay idempotent (ON CONFLICT / upsert) so SaaS-admin edits converge.
- Not all migrations are reversible: `c1e_install_actions` uses `ALTER TYPE ... ADD VALUE` (no downgrade), and `ng2_topology_drop`'s `downgrade()` raises `NotImplementedError` on purpose.
- **DB triggers live only in their Alembic revision, never in SQLAlchemy metadata** — the test suites of every consuming service build schemas with SQLite `create_all`, which cannot parse plpgsql or the PG regex operator `~`. This covers `trg_inventory_item_graph_guard`, `trg_inventory_item_detach_guard` (ng1), `nc1b_device_audit_trigger`, and the two purpose-format CHECKs on the binding tables.
- **`network_graph.MAX_PATH_DEPTH` (32) is hand-kept in sync with the same constant inside `trg_inventory_item_graph_guard`.** If they diverge the trigger wins and traversal starts raising `PATH_TOO_DEEP` on paths the DB accepted.
- **The two token regexes in `provisioning_resolution.py` FAIL OPEN.** Adding a variable namespace without adding it to `_DEVICE_VARIABLE_PATTERN` silently downgrades a fatal resolution error into a half-configured customer. Change them in the same commit.
- Bump `version` in `setup.cfg` for non-trivial changes (NOT `pyproject.toml` — that file only holds build-system config).
- Naming mismatch is historical and intentional: repo `models-utils`, pip package `database-utils`, module `database_utils`.
- Additive schema changes: safe once consuming code is ready. Destructive (drop/rename): all consuming service code must be in production FIRST.
- Test changes from consuming projects (`backend-erp`, `auth-erp`) before publishing.
- Dual-write rollback window is still open: `ClientService` dual-writes into legacy `recurring_order`; don't remove legacy `Product`/`RecurringOrder` paths without checking [docs/limitations.md](docs/limitations.md).

## Recommended Agents and MCP Tools

- **Model/schema implementation**: `python-pro` subagent
- **Library API lookup**: Context7 MCP (`resolve-library-id` → `query-docs`) for SQLAlchemy, Pydantic, and Alembic APIs
- **DB query/performance issues**: `postgres-pro` subagent
- **Python syntax errors** are automatically caught by hooks after every edit
