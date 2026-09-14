# CODEBASE_INDEX — models-utils

Navigation index for the `database-utils` library (v1.14.0, Python >= 3.11).
Not a running service: no endpoints, no ports. Consumed as a pip dependency by
`backend-erp`, `auth-erp`, and `cron-erp`; also built as the one-shot `migrate`
docker-compose service. May drift — verify against actual files.

## Top Level

| Path | Purpose |
|---|---|
| `setup.cfg` | Package metadata (`database-utils`, version), install_requires, `[options.package_data]` for email templates |
| `pyproject.toml` | Build-system config only |
| `Dockerfile` | python:3.12-slim, `pip install .`, CMD `alembic upgrade head` — used only by the repo-root compose `migrate` service |
| `alembic.ini` / `alembic/` | Migration config + 58 revisions (`alembic/versions/`), head `nat3_pylon_socks5` |
| `database_utils/` | The library package (see below) |
| `tests/` | 28 test files, **252 tests** (`pytest.ini`: `asyncio_mode = auto`; in-memory SQLite). `conftest.py` holds the shared `db` + `plant` fixtures (a real in-memory SQLite network graph: `CORE-1 → OLT-1 → SPL-1 → SPL-2 → ONT-1`, splitters passive, ACTIVATION/SUSPENSION bound per device type). **Cycle 10**: `test_network_graph_model.py`, `test_network_graph_traversal.py`, `test_playbook_binding.py`, `test_provisioning_run.py`, `test_network_graph_migrations.py`, `test_isp_seed_passive.py` (`test_topology_purpose.py` / `test_playbook_topology.py` deleted). Also `test_attested_adoption.py` (ba1 guardrails), `test_client_install_field_drop.py` (cf1 guardrails + single-head file scan). **NAT transport (2026-08-13, redesigned 2026-08-17)**: `test_transport_resolver.py` (`resolve_endpoint()` behaviour), `test_nat_transport_constants.py` (model/migration CHECK-fragment parity, nat1 + nat2 + nat3), `test_nat_transport_schemas.py` (`NetworkAccessCreate` validator), `test_nat_transport_gateway_check.py` (whole-branch-review fixes: DB CHECK `ck_network_access_nat_gateway_host`, `NetworkAccessUpdate` validator, `resolve_endpoint`'s cross-tenant `access` guard), `test_nat_zt_pylon_schema.py` (`pylon_socks5` schema exposure) |
| `scripts/resync_billing_cents.sql` | Ad-hoc billing cents resync helper |
| `.github/workflows/ci.yml` | PR/push CI: migration guard, ruff (advisory), pytest on 3.12 |
| `.github/workflows/migrate.yml` | Prod migration on push to `main` (`alembic/**` path filter, `DB_URL` secret) |
| `docs/` | Documentation wiki ([docs/README.md](docs/README.md)) |

## `database_utils/` Package

### Core

| Path | Contents |
|---|---|
| `database.py` | Engine bootstrap: `DATABASE_URL` or `DB_URL` or composed `POSTGRES_*` (raises at import if none); `pool_pre_ping`, `pool_recycle=3600`; exports `SessionLocal`, `Base` |
| `dependencies/db.py` | `get_db` FastAPI session dependency (rollback + close) |
| `dependencies/audit.py` | `get_client_ip` (proxy-aware) |
| `middleware/logging_middleware.py` | `LoggingMiddleware` — request-ID + JWT-context + duration ASGI middleware |
| `constants/roles.py` | `Roles`: ADMIN / MANAGER / SALES / USER |
| `services/email_service.py` | Abstract `EmailService` + Mock + SMTP impl (aiosmtplib); 7 transactional email kinds; the 4 auth kinds (confirmation, invitation, password_reset, welcome) take `locale: str = "es"` as last keyword param with localized Uplink subjects (`_UPLINK_SUBJECTS` es/en dict); selected via `EMAIL_PROVIDER`, `SMTP_USE_TLS` |
| `templates/email/*.html` | 13 Jinja2 templates: `base_uplink` (Uplink-branded shell) + es/en pairs for confirmation/invitation/password_reset/welcome; legacy `base_layout`, join_request_decision, payment_failed, payment_receipt. `render_email(name, locale)` resolves `{stem}.{locale}.html` → `{stem}.es.html` → `{stem}.html` |

### Models (`models/` — all UUID v4 PKs, created_at/updated_at; registered in `models/__init__.py` for Alembic autogenerate)

| Module | Models (table names) |
|---|---|
| `auth.py` | Tier (tier), Company (company), Permission (permission), Role (role), User (user), Notification (notification), AuditLog (audit_log), UserInvitation (user_invitation), EmailVerificationToken (email_verification_token), PasswordResetToken (password_reset_token), Subscription (subscription), PaymentMethod (payment_method), BillingInvoice (billing_invoice), BillingWebhookEvent (billing_webhook_event — rb1, `svix_id` string PK + `event_type`/`created_at`, Recurrente webhook delivery idempotency log); **Recurrente billing (rb1)**: `Tier.recurrente_product_id`/`recurrente_price_id`/`recurrente_price_yearly_id` (NULL price id = not purchasable online), `Company.recurrente_customer_id` (lazy, first checkout), `Subscription.recurrente_subscription_id` (unique)/`recurrente_checkout_id`/`card_last4`/`card_brand`, `BillingInvoice.recurrente_intent_id` (unique — webhook charge idempotency). `TierChangeRequest` model removed (superseded by Recurrente self-serve checkout/cancel); its `tier_change_request` table is still physically present pending a later destructive-change release |
| `crm.py` | Client (client — `installation_status`/`installation_date` + the `InstallationStatus` enum DROPPED by `cf1_drop_client_install_fields`; install truth is `client_service.install_state`, lists derive `services_total`/`services_installed` rollups in backend-erp), Product (product — legacy, absorbed by catalog merge), Order (order), OrderItem (order_item), RecurringOrder (recurring_order) + RecurringOrderItem (legacy billing, dual-written; consumed by cron-erp), Invoice (invoice), Payment (payment — Cycle 1 ledger), CustomFieldDefinition / ClientCustomFieldValue, TaskState (task_state), Task (task, `task_assignee` M2M), TaskTemplate (task_template), Integration (integration) |
| `isp.py` | ServicePlan (service_plan), ClientService (client_service), ServiceSuspension (service_suspension), DeviceCategory (device_category — global SaaS-admin table), DeviceType (device_type), Warehouse (warehouse), InventoryItem (inventory_item), EquipmentEvent (equipment_event), Playbook (playbook), **DeviceTypePlaybook (device_type_playbook)**, **InventoryItemPlaybook (inventory_item_playbook)**, **ProvisioningRun (provisioning_run)**, ProvisioningJob (provisioning_job); **Cycle 4 insights**: InsightDashboard (insight_dashboard), InsightChart (insight_chart); **Cycle 5 network config**: NetworkAccess (network_access), DeviceCredential (device_credential), AcsDeviceRegistration (acs_device_registration), ProvisioningSettings (provisioning_settings), DeviceActionLog (device_action_log); **Cycle 7 core config (nc2a)**: no new tables — new columns on existing ones: `device_category.tier` (CORE/EDGE), `device_type.cli_platform`, `inventory_item` mgmt surface (`mgmt_host`/`mgmt_port`/`cli_protocol`/`mgmt_last_check_at`/`mgmt_last_check_ok`), `client_service.install_state` + `installed_at`; **Cycle 8 (c8a)**: `Playbook` drops `target_vendor`/`target_category_id` (+ FK) — the `topology_id` it added was dropped again in Cycle 10; **Cycle 10 network graph (ng1/ng2, doc 35)**: `Topology`/`TopologyDeviceType`/`TopologyPlaybook` **deleted**; `InventoryItem` gains `parent_id` (self-FK RESTRICT, indexed) + `network_attached` (bool) + `parent`/`children` relationships + CHECKs `ck_inventory_item_parent_attached`/`ck_inventory_item_not_self_parent` + partial index `ix_inventory_item_company_attached`; `DeviceCategory.is_passive`; `ClientService` gains `cpe_item_id` (FK inventory_item SET NULL, indexed) + `path_changed_at` and **drops** `topology_id`; `ServicePlan` drops `default_topology_id`; `Playbook` drops `topology_id`; `ProvisioningJob` gains `run_id` (FK provisioning_run CASCADE, indexed) + `run_position`; purpose constants renamed `CANONICAL_TOPOLOGY_PURPOSES` → `CANONICAL_PLAYBOOK_PURPOSES` and `TOPOLOGY_PURPOSE_PATTERN` → `PLAYBOOK_PURPOSE_PATTERN`; **Brownfield adoption (ba1)**: `ClientService` gains `adopted_at`/`adopted_by_user_id` (FK → user, SET NULL)/`adoption_note` + partial index `ix_client_service_adopted`, plus `ACTIVATION_EVIDENCE_*` constants for the backend-computed `activation_evidence` field; **Per-service provisioning parameters (sp1)**: `ClientService` gains `provisioning_params` (JSON NULL, `[{key, value}]`) holding this service's values for the parameters its plan declares with `scope='service'`; `ServicePlan.provisioning_params` rows gain an optional `scope` (`plan`|`service`, absent = plan) — both resolve to `{{service_plan.<key>}}`, and `connection_params` stays free-form and is NOT a variable source; **NAT transport (nat1, 2026-08-13)**: `NetworkAccess.gateway_host` (String, nullable), `InventoryItem.nat_port` (Integer, range-CHECKed, unique per company where set) + `InventoryItem.mgmt_host_key` (String, nullable, pinned SSH host key); `NETWORK_ACCESS_MODES` widens to include `nat_zt`/`nat_public`, `NAT_MODES = ("nat_zt", "nat_public")`. See [docs/network-models.md](docs/network-models.md) |
| `workflow.py` | WorkflowTemplate (workflow_template), Workflow (workflow), WorkflowTrigger (workflow_trigger), WorkflowStep (workflow_step), WorkflowStepEdge (workflow_step_edge), WorkflowExecution / WorkflowStepExecution |

Key enums: `OrderStatus`, `OrderType`, `PaymentStatus`, `PaymentKind`, `PaymentMethodType`, `RecurrenceEnum`, `ServiceAvailability` (`InstallationStatus` removed by cf1), `TaskStateColor`, `TaskLinkedObjectType`, `IntegrationAuthType`, `ServicePlanType`, `CatalogKind`, `ClientServiceStatus`, `SuspensionReason`, `ProvisioningJobStatus` (incl. Cycle-5 `PENDING_INFORM`), `ProvisioningTrigger`, `TriggerEventType`, `StepActionType`, `ExecutionStatus`, `InsightChartType`. Cycle-5/7 driver-bounded value sets are CHECK-constrained strings, not PG enums: `CREDENTIAL_KINDS`, `NETWORK_ACCESS_KINDS`, `NETWORK_ACCESS_MODES`; Cycle 7 adds `DEVICE_CATEGORY_TIERS` (CORE/EDGE), `CLI_PROTOCOLS` (ssh/telnet), `INSTALL_STATES` (NOT_INSTALLED/IN_PROGRESS/INSTALLED). Purposes stay plain strings, not a PG enum (tenants define their own): `PURPOSE_ACTIVATION`/`SUSPENSION`/`REACTIVATION`/`DEPROVISION`, `CANONICAL_PLAYBOOK_PURPOSES` and `PLAYBOOK_PURPOSE_PATTERN` — renamed from `CANONICAL_TOPOLOGY_PURPOSES`/`TOPOLOGY_PURPOSE_PATTERN` in Cycle 10, since purposes now key playbook bindings rather than topologies.

### Schemas (`schemas/` — 41 modules, Pydantic v2; `__init__.py` star-imports + `model_rebuild()`)

client, company, custom_field, invoice, notification, order, order_item, payment,
permission, product, recurring_order, requests (Login + flat company-only Signup), role, task,
task_state, task_template, user, workflow, integration, service_plan,
client_service, inventory, playbook, workflow_template, tier,
subscription, payment_method, billing_invoice, invitation,
audit_log, device_category, email_verification, password_reset, pagination
(`PaginatedResponse[T]`); **Cycle 4**: insight; **Cycle 5**: acs_registration,
device_credential, network_access, provisioning_settings. (`schemas/network.py`
— the old graph schema — was deleted with the graph removal (c2d); unrelated to
the Cycle-5 network-config schemas above.) **Cycle 7** adds no modules — it
extends `device_category` (tier), `inventory` (cli_platform + mgmt surface),
`playbook` (`target_item_id`, `ping` driver), `client_service` (read-only install
state).
**Cycle 8** (c8a) further edits `playbook`: drops `target_vendor`/`target_category`
(`PlaybookBase`) and `target_category_id` (`PlaybookOut`).
**Cycle 10** (ng1/ng2, doc 35) **deletes `schemas/topology.py`** and moves its
`normalize_purpose` helper into `schemas/playbook.py` (validating against the
renamed `PLAYBOOK_PURPOSE_PATTERN`); `playbook` loses `PlaybookOut.topology_id`
and `PlaybookStep.target_position` — a playbook binds to one device type and runs
on one device, so the executor defaults the step target to `{{device.item_id}}`.
`client_service` swaps `topology_id` for `cpe_item_id` + write-only
`cpe_parent_id` (Base/Update) and adds read-only `path_changed_at` (Out);
`ClientServiceAdoptIn` drops `topology_id`. *Known drift:* `service_plan` still
declares an inert `default_topology_id` with no backing column — see
[docs/limitations.md](docs/limitations.md).
**Brownfield adoption** (ba1) adds no modules — `client_service` gains the
read-only adoption fields (`adopted_at`/`adopted_by_user_id`/`adoption_note` +
backend-computed `activation_evidence`) and the `ClientServiceAdoptIn` /
`ClientServiceAdoptBulk*` request/response shapes.
**Recurrente billing** (rb1) adds no modules — `SubscriptionOut` gains
`recurrente_subscription_id`/`card_last4`/`card_brand`; `TierOut` gains the three
`recurrente_*` ids; `TierPublic` gains a stamped `purchasable: bool` (endpoint-computed
from `recurrente_price_id` presence — never exposes the raw price id).
**NAT transport** (nat1, 2026-08-13) adds no modules — `network_access` gains `gateway_host` (Optional[str]) plus a `model_validator` requiring it when `mode` is `nat_zt`/`nat_public`; `inventory` gains `nat_port` (`Field(ge=1, le=65535)`, same pattern `mgmt_port` now also uses) on `InventoryItemBase`/`InventoryItemUpdate`. See [docs/network-models.md](docs/network-models.md).
**Auth overhaul** (no migration): `requests.SignupCompanyRequest` flattened to
`{company_name, name, email, password (min 8), locale}` and `SignupUserRequest`
**deleted** (users are invitation-only); `locale: Literal["es","en"] = "es"`
added to `ResendConfirmationRequest`, `PasswordResetRequestSchema`, and
`InvitationCreate`; `InvitationAccept` is `{token, name, password (min 8)}`
(`age` removed); `PasswordResetConfirmSchema.new_password` min raised 6 → 8 —
see [docs/schemas.md](docs/schemas.md) + [docs/email-service.md](docs/email-service.md).

### Utilities (`utils/` — 25 modules)

| Module | Role |
|---|---|
| `workflow_engine.py` (largest file) | Trigger matching + async DAG execution (`check_workflow_triggers`, `execute_workflow`, `execute_step`). `ENQUEUE_PROVISIONING` mode A key is **`use_service_path`** (`use_topology` raises); mode A opens a `ProvisioningRun` |
| `network_graph.py` | **Cycle 10** — the only walker of the `inventory_item.parent_id` tree: `MAX_PATH_DEPTH = 32`, `GraphError`, `resolve_path` (leaf → root), `descendants`, `would_create_cycle`, `child_count`. Company-scoped recursive CTEs (both anchor **and** recursive term), depth-bounded, SQLAlchemy Core so it runs on SQLite too |
| `provisioning_resolution.py` | **Cycle 10 rewrite** — starts at `client_service.cpe_item_id`, walks to the root, skips `is_passive` nodes, resolves each remaining node's playbook (node override → device-type default → none, `resolve_playbook_for`). Returns `ResolvedNode` / `ResolvedProvisioning(path, steps, shared_variables, device_variables)`. Codes: `CPE_NOT_SET`, `CPE_NOT_ATTACHED`, `PLAYBOOK_NOT_BOUND`, `PLAYBOOK_INACTIVE`, `RESOLUTION_FAILED`, `PATH_TOO_DEEP` (gone: `MISSING_DEVICE`/`AMBIGUOUS_DEVICE`/`PINNED_DEVICE_UNAVAILABLE`/`TOPOLOGY_NOT_SET`/`TOPOLOGY_INACTIVE`/`PURPOSE_NOT_CONFIGURED`). Namespaces `device.*` / `cpe.*` / `path.<category>.*` / `service_plan` / `client` / `service` / `input`; `DEVICE_ATTRIBUTES` + `build_device_frame`; flat dotted keys, no nesting |
| `provisioning_runs.py` | **Cycle 10** — `create_run` (resolve once, snapshot `path`/`plan`/`frames`, queue child 0), `advance_run` (next child on SUCCEEDED; any other terminal status stops the run; clears `path_changed_at` on a non-dry-run ACTIVATION), `find_in_flight_run`, `run_idempotency_key`. Standalone jobs (`run_id` NULL) are untouched |
| `transport.py` | **2026-08-13, doc 34 R23 rewrite; pylon redesign 2026-08-17** — `resolve_endpoint(db, item, company_id, default_port, access=None)`: the one place `InventoryItem` + the tenant's default `kind='olt'` `NetworkAccess` row become a dial `ResolvedEndpoint(host, port, proxy, mode)`. `direct` unchanged; `nat_zt`/`nat_public` target `(access.gateway_host, item.nat_port)`, never `item.mgmt_host`; missing mapping → `NAT_MAPPING_NOT_SET`; `nat_zt` reads `access.pylon_socks5` (per-tenant column, `nat3_pylon_socks5` — not a function argument) and fails closed with `PYLON_NOT_PROVISIONED` if blank/NULL. Both NAT modes are dial-capable |
| `jwt_utils.py` | HS256 create/decode; `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE`, `REFRESH_TOKEN_EXPIRE`; fails fast in production if `SECRET_KEY` unset |
| `permission_utils.py` | `PermissionChecker` / require-permission dependencies |
| `audit_utils.py` | log_create/update/delete/custom operation helpers |
| `ssrf.py` | `validate_url_no_ssrf` blocklist (integrations + workflow HTTP_REQUEST) |
| `crypto.py` | AES-256-GCM envelope encryption for device credentials (Cycle 5): `CREDENTIALS_KEKS`, `CREDENTIALS_ACTIVE_KEK_ID`; wrap/unwrap DEKs, KEK rotation |
| `workflow_fields.py` | Trigger-context variable/field handling |
| `tier_limits.py`, `pagination_utils.py`, `timezone_utils.py`, `token_utils.py`, `password.py`, `order_typing.py`, `json_utils.py`, `email_templates.py`, `error_handling.py`, `exception_handlers.py`, `logging_utils.py`, `telemetry_utils.py`, `router_factory.py` | Supporting helpers (see [docs/utilities.md](docs/utilities.md)) |

## Alembic (`alembic/`)

`env.py` imports all four model modules; after `upgrade` it runs `_run_seeds(connection)`:
`alembic/seeds/rbac_seed.py`, `alembic/seeds/tier_seed.py`, `alembic/seeds/isp_seed.py` — all idempotent (importable as `seeds.*` because `env.py` adds the alembic dir to `sys.path`).

Notable revision chains (base: `f612571eaad0_initial_schema_with_uuid`):

- **Cycle 1 billing**: `c1a_billing_ddl` → `c1b_backfill` → `c1c_payment_ledger` → `c1e_install_actions` (**irreversible** ALTER TYPE) → `c1f_verify_grandfather`
- **Cycle 2 merge/topology**: `c2a_catalog_merge` → `c2b_service_billing` → `c2c_topology_device_chain_playbook` → `c2d_graph_removal` → `c2e_step_exec_snapshot`
- **Cycle 3**: `c3a_topology_purpose_playbooks`, `c3b_device_categories_global_table`
- **Cycle 4 insights**: insight dashboard/chart tables
- **Cycle 5 network config**: `nc1a` (five network tables + `ProvisioningJob` columns + `PENDING_INFORM` via `ALTER TYPE … ADD VALUE` + 17 permissions) → `nc1b` (append-only `device_action_log` trigger)
- **Cycle 7 core config**: `nc2a_core_config` (hand-written, additive, guarded/idempotent: `device_category.tier` + backfill, `device_type.cli_platform`, `inventory_item` mgmt surface, `topology_device_type.inventory_item_id` FK SET NULL — since dropped with the table by ng2, `client_service.install_state`/`installed_at`; three new CHECK constraints)
- **NAT transport**: `nat1_gateway_transport` (on `ng2_topology_drop`, hand-written, additive) — `network_access.gateway_host`, `inventory_item.nat_port` + `mgmt_host_key`, widened `ck_network_access_mode` CHECK, new `ck_inventory_item_nat_port`/`ck_inventory_item_mgmt_port` CHECKs, partial-unique `uq_inventory_item_company_nat_port`; `downgrade()` refuses if any row is in a NAT mode → `nat2_gateway_host_check` (hand-written, additive) — DB-level CHECK `ck_network_access_nat_gateway_host` (`mode NOT IN ('nat_zt','nat_public') OR gateway_host IS NOT NULL`), closing the gap where `NetworkAccessUpdate` had no cross-field validator; scrubs any pre-existing NAT row with no `gateway_host` back to `direct` before adding the constraint → `nat3_pylon_socks5` (head, hand-written, additive) — `network_access.pylon_socks5` (String, nullable) + DB-level CHECK `ck_network_access_pylon_socks5` (`mode != 'nat_zt' OR pylon_socks5 IS NOT NULL`); retracts the shared-fleet-Pylon design (doc 34 OV17) in favor of a per-tenant proxy column, replacing the removed `PYLON_SOCKS5` worker env var (spec N4)
- **Grandfathered verification**: `t2_grandfather_email_verified` — backfills `user.email_verified = TRUE` for all pre-overhaul users (login-gate lockout guard)
- **Brownfield adoption**: `ba1_attested_adoption` — additive `client_service` adoption columns + FK + partial index `ix_client_service_adopted` + ADMIN-only `client_services.adopt` permission (MANAGER excluded at both seed auto-grant sites: `isp_seed.ADMIN_ONLY_PERMISSIONS` / `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS`)
- **Client install-field removal**: `cf1_drop_client_install_fields` — data cleanup BEFORE DDL (deletes installed `UPDATE_FIELD` workflow steps writing the dropped fields with edge rerouting; deletes clients insight charts using the `installation_status` dimension/filter), then drops `client.installation_status`/`installation_date` and the `installationstatus` PG enum; downgrade recreates structure only (data not restorable)
- **Free/Trial unlimited**: `t1_free_trial_unlimited` — data migration setting Free/Trial tier features to unlimited (-1) and modules to the full set; paired with the updated `tier_seed.py` (fresh installs seed the same, `modules` column now seeded)
- **Recurrente tenant billing**: `rb1_recurrente_billing` (parent `cf1`) — additive only: `recurrente_*` gateway columns on tier/company/subscription/billing_invoice (unique on `subscription.recurrente_subscription_id` and `billing_invoice.recurrente_intent_id`) + new `billing_webhook_event` table (svix-id PK webhook delivery idempotency log)
- **New-installation template v4**: `tk1_new_installation_v4` (parent `rb1_recurrente_billing`) — schema **no-op**; exists so the path-filtered prod `migrate.yml` workflow replays seeds. Ships the `isp_seed.py` change: the `new-installation` blueprint's installation-fee param becomes type `service_plan` (key `installation_fee_plan_id`) and the CREATE_ORDER item uses `service_plan_id` — the old required `product` param pointed at the retired legacy Product catalog (no create path), blocking fresh tenants; installed v3 copies keep running (`product_id` honored during the rollback window)
- **Cycle 8 topology-owned playbooks**: `c8a_playbook_topology` (hand-written, guarded/idempotent with in-migration assertions) — drops `playbook.target_vendor` + `playbook.target_category_id` (+ its FK), adds `playbook.topology_id`; the `topology_id` half is **undone by `ng2_topology_drop`**
- **Service lifecycle**: `sp1_service_params` → `bf1_topology_backfill` → `lc1_retire_removal_tmpl` → `lc2_retire_susp_react`
- **Cycle 10 network graph (doc 35)**: `ng1_network_graph` → `ng2_topology_drop` (**head**), both on `lc2_retire_susp_react`.
  - `ng1` — strictly additive: `inventory_item.parent_id`/`network_attached` (+2 CHECKs, +2 indexes, self-FK RESTRICT), `device_category.is_passive`, the `device_type_playbook` + `inventory_item_playbook` binding tables (purpose CHECK migration-only), `client_service.cpe_item_id`/`path_changed_at`, `provisioning_run` + `provisioning_job.run_id`/`run_position`, and the two plpgsql guards `trg_inventory_item_graph_guard` (self-parent / cross-tenant / detached-parent / cycle / depth ≥ 32) and `trg_inventory_item_detach_guard` (detach with children). Triggers live only in the revision — SQLite `create_all` cannot parse plpgsql
  - `ng2` — guard → rewrite → drop, **irreversible** (`downgrade()` raises). Guards raise on any playbook still containing `chain[` / `edge_devices[` / `core_devices[` / `RETIRED_ALIAS` / `target_position`, and on any `client_service` with `topology_id` but no `cpe_item_id` (**no chain→graph backfill by design**). Rewrites `use_topology` → `use_service_path` in `workflow_step.action_config` + `workflow_template.definition` (predicate-guarded, byte-identical on re-run). Drops `client_service.topology_id`, `service_plan.default_topology_id`, `playbook.topology_id`, then `topology_playbook`, `topology_device_type`, `topology`
- **ISP core**: `cd2f0076c709_isp_platform_core_service_plans_`; plus tenant indexes (`a1f2b3c4d5e6`), timezone fixes, task/workflow/integration modules

## Relationships

- `backend-erp` — pins by SHA; uses models, schemas, `get_db`, permission/audit utils, workflow engine, provisioning models/resolution
- `auth-erp` — pins by SHA; uses auth models/schemas, jwt_utils, email service + templates, SaaS billing models
- `cron-erp` — pip dependency; RecurringOrder models
- `frontend-erp` — no direct dependency; consumes JSON shaped by these schemas via backend proxies
- Repo-root `docker-compose.yml` — `migrate` service builds this repo's Dockerfile
