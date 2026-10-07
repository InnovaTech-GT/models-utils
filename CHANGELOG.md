# Changelog

All notable changes to the `database-utils` library will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **BREAKING (6.0.0)** - `task_assignee` is now the mapped model `TaskAssignee` (`__tablename__ = "task_assignee"`), like the rest of the tables, instead of a bare `Table`. Same columns, primary key and `ck_task_assignee_role`, so no DDL; Alembic `ta1_task_assignee_model` (**new head**, on `pc1_provisioning_claim_token`) only asserts the table shape. `Task.assignees` keeps working through `secondary="task_assignee"`. Breaking for importers: `database_utils.models.task_assignee` is gone; use `TaskAssignee` (`TaskAssignee.role`, `insert(TaskAssignee)`, `TaskAssignee.__table__` where a `Table` is needed).

### Fixed
- **5.1.0** - provisioning concurrency (release-v1.0.0/provisioning-concurrency; Alembic `pc1_provisioning_claim_token`, additive, metadata-only, on `pt2_unmap_port_labels`). `provisioning_job.claim_token` UUID NULL: the worker's per-claim fence token. **Producers never lock:** `_queue_child` inserts every run child with `device_lock_key` NULL (`_device_lock_key` deleted) — the lock is written only by the worker claim, so settling child N no longer rolls back when child N+1's device is busy (the 2026-10-06 incident). `advance_run` locks the run row (`FOR UPDATE`, `populate_existing`) and no-ops on a terminal run, on an in-flight job, and when a later child already exists (duplicate/stale advance). New `create_or_get_run(db, svc, ...) -> (run, created)`: dedupes on the run key and inserts in a SAVEPOINT, so a lost race returns the winner instead of an IntegrityError. New `repair_stranded_runs(db, limit=100)`: advances in-flight runs with no in-flight child (quiet > 30 s, `FOR UPDATE SKIP LOCKED`, one savepoint per run); a run whose last child finished over an hour ago (`STRANDED_RUN_MAX_AGE`) is closed FAILED instead (`STRANDED_RUN_EXPIRED`), so the first deploy does not resume runs stranded long before it. **Key change:** `run_idempotency_key` is now the single shared key — `deprovision-{id}` for DEPROVISION, else `path-provision-{id}-{purpose}`, `-dry` suffix (was `path-{id}-{purpose}`); the workflow engine's `ENQUEUE_PROVISIONING` (service path) uses `create_or_get_run` and returns `deduped: true` with the existing `run_id`, so it dedupes against `/provision` and the lifecycle hooks. New `tests/pg/test_provisioning_runs_pg.py`.
### Removed
- **BREAKING (5.0.0)** - `InventoryItem.parent_port` / `uplink_port` and the `uq_inventory_item_parent_port` Index are no longer mapped (doc 40 §4.2 C8a; Alembic `pt2_unmap_port_labels` on `vw1_viewer_no_credential_read` drops that index and nothing else). The lp1 free-text labels are superseded by `network_link` + `inventory_item_port`; the index goes now because a C8a backend no longer clears a re-parented item's label, so a move next to a sibling with the same legacy label would raise a unique violation (an old backend still checks label uniqueness in code). The DB keeps the columns until C8b (`pt3_drop_port_labels`), which must not migrate a database before every backend deployed on it runs C8a. Consumers that read or write the attributes must drop them first (backend-erp: `PATCH /network/nodes/{id}/link` and the legacy-label holder logic).

### Security
- **4.5.2** - `computed` block hardening (ADR-006 integration review F1–F3). F1: `PlaybookDefinition` refuses any `{{computed…}}` token that is not exactly `{{computed.<key>}}` (+ filters) — `{{computed.onu.y}}`/`{{computed[0].x}}` could otherwise be filled by a caller-supplied value. F2: the resolver's `| default:` detection ignores quoted filter arguments (`replace:"|default:","x"` no longer skips the up-front refusal). F3: `evaluate_all` reports malformed stored blocks/entries (non-list, non-dict, non-string key/expr, non-int min/max) as `COMPUTE_SYNTAX`/`COMPUTE_TYPE` instead of raising.

### Added
- **4.5.1** - VIEWER no longer reads device credentials (Alembic `vw1_viewer_no_credential_read`, data-only, on `cc1_client_code`). Deletes the global VIEWER role's `device_credentials.read` grant; `rbac_seed.VIEWER_PERMISSION_FILTER` now excludes it so the post-upgrade seed does not re-grant it. Founder decision D2 of the v1.0.0 release plan. `downgrade()` re-grants it.
- **4.5.0** - client short codes (Alembic `cc1_client_code`, additive). `client.code` VARCHAR(16) NOT NULL: a per-company unique id, case-insensitive (`uq_client_company_code` on `(company_id, upper(code))`), format `^[A-Z0-9-]{1,16}$` (`ck_client_code_format`, Alembic-only). Backfilled from `[LEGACY_ID:<code>]` observation tags where unique per company, otherwise random. New clients get a random 6-char code (`utils/client_code.py`: `generate_client_code`, `normalize_client_code`; DB default `client_code_generate()` for writers that predate the column). `ClientCreate/ClientUpdate.code` (optional override, normalized) and `ClientOut.code`. Playbook variable `client.code`.
- **4.4.0** - port-level topology, schema half (doc 40 §3.1, §3.3.3; Alembic `pt1_port_topology`, additive and inert). `device_type.port_template` (JSON, `none_as_null`) + `path_role` + `ck_device_type_ports_serialized`; `inventory_item` `uq_inventory_item_id_company`; new models `InventoryItemPort` (`inventory_item_port`: name/slot/number/medium/direction/origin, composite FK to the item, `uq_item_port_name` on `lower(name)`, partial `uq_item_port_pon_number`) and `NetworkLink` (`network_link`: one upstream link per device, composite port/item/company FKs, port FKs NO ACTION); two deferred constraint triggers (`trg_network_link_parent_sync`, `trg_inventory_item_link_sync`, Alembic-only) raising `NETWORK_LINK_PARENT_MISMATCH` at COMMIT when a link disagrees with `parent_id`; `downgrade()` refuses while links or ITEM ports exist. Schemas: `PortTemplateGroup`, `PortSpec`, `expand_port_template`, `validate_port_template`, `normalize_path_role`, `path_role_shadows_category` (`schemas/inventory.py`); `DeviceTypeCreate/Update/Out.port_template` + `path_role` (`PORT_TEMPLATE_REQUIRES_SERIALIZED`). New `utils/playbook_expr.py` (declared integer arithmetic: `parse`, `names`, `evaluate_all`; codes `COMPUTE_*`) and `is_secret_name`/`_SECRET_HINTS` moved here from backend-erp's renderer; `ComputedVar` + `PlaybookDefinition.computed` with save-time checks. Hash-locked fixture `tests/fixtures/playbook_expr.json`; `tests/pg` + CI job `pg` for the Postgres-only guarantees.
- **4.4.0** - port-level topology, resolver half (doc 40 §3.3.1–3.3.2; no schema change). `ResolvedNode` gains `label`, `path_role`, `out_slot`/`out_port`/`out_port_name` (from the link below, one company-scoped query, only if `link.up_item_id` is this node; never on the CPE) and `playbook_version`; `ResolvedProvisioning.ambiguous_roles`. `PORT_ATTRIBUTES` emitted by `build_device_frame` only when known. `path.<role>.*` frames for roles held by exactly one node; `ROLE_SHADOWS_CATEGORY`. **Behaviour change for every purpose:** resolution evaluates each step's `computed` block and every resolver-owned token rendered before a step (skipping `| default:`), refusing up front with `RESOLUTION_FAILED` + `ROLE_AMBIGUOUS` / `PORT_NOT_RECORDED` / `UNRESOLVED_TOKEN` / `COMPUTE_*`; non-ACTIVATION purposes also get `PATH_CHANGED_SINCE_ACTIVATION` against the last SUCCEEDED non-dry ACTIVATION run's frames. `provisioning_run.plan` entries carry `playbook_version`. Fail-open fix: `_playbook_references_device_variables`/`_playbook_references_token` read `computed` operands; an unparseable block counts as referenced. New test helper `network_graph.assert_links_consistent`.

### Fixed
- **4.3.1** - data repair `sh1_service_history_repair` (one-way, no schema change): reconstructs client-service history for plan changes the ISP adoption import missed. For services whose RECURRING orders switch once from an older price to the current plan's price, each older run becomes a CANCELLED historical `client_service` (`migration_source='sh1'`, on the single SERVICE plan with that price) and its orders + line items move to it; the current service's `activation_date`/`created_at` move to its first current-price order. Other mismatched single-line orders get their line price set to the order total. Services left ACTIVE with billing INACTIVE after being replaced by a later service are cancelled at the replacement's start (`recurrence_end` = their last billed order). Order totals and payments are untouched.

### Added
- (4.3.0) Alembic `cr1_cash_review`: `CashSessionStatus` SUBMITTED/REJECTED/APPROVED; `cash_session.submitted_at/reviewed_at/reviewed_by/review_note`; permission `cash_sessions.review` (ADMIN); `CashSessionOut` review fields.
- (4.2.0) Alembic `pd1_client_payment_day`: nullable `client.payment_day` (1..31, CHECK) on model + `ClientBase`/`ClientUpdate`/`ClientOut`.
- (4.1.0) Alembic `mp1_technician_plan_read`: grants `service_plans.read` to the system TECHNICIAN role (install-order plan picker in uplink-mobile tecnicos); `isp_seed.ISP_ROLES` updated.

### Removed
- **BREAKING (4.0.0)** - legacy drop (Alembic `ld1_legacy_drop`, destructive, no downgrade). Models/tables `Product`, `RecurringOrder`, `RecurringOrderItem`, `TaskState` (+`TaskStateColor`, `TASK_STATE_KINDS`), `WorkflowTemplate`; columns `order.recurring_order_id`, `order_item.product_id`, `service_plan.product_id`, `client_service.recurring_order_id`, `task.task_state_id`; schemas `product`, `recurring_order` (kept DTOs moved to `schemas/billing_due.py`), `task_state`, `workflow_template`; `OrderOut.recurring_order`/`generation_period`; `TaskLinkedObjectType.RECURRING_ORDER`; permissions `products.*`, `recurring_orders.*`, `task_states.*`, `workflow_templates.*` (grants copied to `service_plans.*`/`client_services.*` first); the workflow-template seed catalog; `CREATE_ORDER` `product_id` items and `CREATE_TASK` `task_state_id`; `KNOWN_RESOURCE_TYPES` `product`/`task_state`/`recurring_order`. Unbridged ACTIVE recurring orders are migrated into `client_service`; legacy workflows are deleted. `OrderItemBase.service_plan_id` is now required. `alembic/env.py` seed sentinel is `client_service`.

### Changed
- **BREAKING (3.0.0)** — built-in roles collapse to ADMIN / VIEWER / COLLECTOR / TECHNICIAN (Alembic `rr1_four_builtin_roles`). `Roles.MANAGER`, `Roles.SALES`, `Roles.USER` are removed; `Roles.VIEWER`, `Roles.COLLECTOR`, `Roles.TECHNICIAN` are added. New permission `web.access` (web dashboard gate). Holders of the removed roles are remapped (MANAGER→ADMIN, BILLING→COLLECTOR, others→VIEWER); seeds no longer create MANAGER/SALES/USER/NOC/WAREHOUSE/SUPPORT/BILLING; `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS` and `isp_seed.ADMIN_ONLY_PERMISSIONS` are gone.

### Security
- Only the **global** ADMIN role (`company_id IS NULL`) gets the `*` wildcard (`PermissionChecker`) or passes `get_admin_user` / `require_roles`; a tenant custom role named "ADMIN" (or any built-in name) no longer escalates. rr1 renames such existing custom roles to `"<name> (custom)"`.
- Alembic `ci1_category_icons` (data-only): default `device_category.icon` for ROUTER `radio-tower` -> `router` and OLT `radio` -> `server` (customised icons untouched); seed updated to match.

### Fixed
- Workflow engine `CREATE_TASK` assigns only company users holding the TECHNICIAN role (tasks are for technicians; mirrors backend-erp's 422 `ASSIGNEE_NOT_TECHNICIAN`). Rejected ids are skipped and reported in the step result (`skipped_assignee_ids`, `warning`) instead of failing the run; the task is created unassigned (PENDING) if nobody qualifies. No migration.

## [2.3.1] - 2026-09-30

Bug fix `refresh-token-reuse`: a rotated refresh token stayed valid until expiry.

### Added
- Alembic `rt1_auth_refresh_token` (additive): `auth_refresh_token` table, model `RefreshToken`.
- Refresh JWTs carry a `jti` claim; `create_refresh_token(..., jti=None)`.

## [2.3.0] - 2026-09-29

Mobile integration (feature `mobile-integration`): uplink-mobile cobros and
tecnicos on the real system. Built on the fixed-task-status and
dispatch-routes cycles, with `origin/develop` (lp1_link_ports) merged in.

### Added
- Alembic `mi1_mobile_enum_labels` (merge of `dr1_task_route_sequence` + `lp1_link_ports`): `TaskJobKind.RELOCATION`, `CashSessionStatus.DEPOSITED`, `EquipmentEventType.CONSUMED` / `.RELEASED`.
- Alembic `mi2_mobile_field_ops` (**new head**, additive): `Task.started_at`/`completed_at`/`step_progress`; `TaskMaterial`; `InventoryItem` + `Warehouse` coordinates; `UserNotification`; `CashSession` opening/deposit/reopen columns; `CashMovement`; `Payment.allocation_id`/`cash_session_id`; `UploadedFile.idempotency_key`; `Company.mobile_settings`; `ix_order_open_receivables`. COLLECTOR gains `mobile.collector`, `tasks.create`, `service_plans.read`, `inventory_items.read`.
- Schemas: `idempotency_key` on `PaymentCreate`/`FullPaymentCreate`; `allocation_id`/`cash_session_id` on `PaymentOut`; coordinates on inventory item/warehouse schemas; `BankAccount` + `MobileSettings` on `CompanyUpdate`/`CompanyOut`; `LoginRequest.client_type`.
- JWT `type` claim (`access`/`refresh`), refresh `cl` claim, `is_refresh_payload()`, `MOBILE_ACCESS_TOKEN_EXPIRE`, `create_access_token(expires_minutes=)`, `create_refresh_token(client_type=)`.

### Changed
- `get_current_user` / `require_permission` reject refresh tokens (401 `Invalid token type`); `require_permission` returns 403 for an inactive user.
- `decode_token` and `require_permission` no longer log tokens or payloads.

## [2.2.0] - 2026-09-28

Dispatch routes (feature `dispatch-routes`).

### Added
- `Task.route_sequence` (INTEGER, nullable): the stop's order in its technician's route for `scheduled_date`, written by backend-erp's `POST /dispatch/routes` and read by the technician app. NULL means not routed. It is not `position`, which the move and reorder endpoints renumber.
- `TaskOut.route_sequence` (read-only, not on `TaskUpdate`).
- Alembic revision `dr1_task_route_sequence` (parent `ts1_task_status`, **new head**), additive and reversible. No index: `ix_task_company_scheduled_date` covers the reads.
- `tests/test_task_route_sequence.py`.

## [2.1.0] - 2026-09-28

Fixed task status (feature `fixed-task-status`). Every tenant's tasks use the
same four statuses instead of tenant-defined board columns.

### Added
- `Task.status` (VARCHAR(20), NOT NULL, default `PENDING`), CHECK `ck_task_status` over `TASK_STATUSES = ("PENDING", "ASSIGNED", "IN_PROGRESS", "DONE")`, index `ix_task_company_status`.
- `utils/task_status.py`: `derive_status()` (PENDING and ASSIGNED follow the technician assignment, IN_PROGRESS and DONE are only set explicitly) and `STATUS_FROM_STATE_KIND` (CANCELLED maps to DONE).
- `schemas/task.py`: `TaskStatus` Literal, and `status` on `TaskCreate`, `TaskUpdate` and `TaskOut`.
- Alembic revision `ts1_task_status` (parent `tr1_transport_axis`, **new head**). It backfills `status` from each task's `task_state.kind` (ASSIGNED without a technician becomes PENDING, CANCELLED becomes DONE), makes `task_state_id` nullable and rewrites installed workflows that reference task_state UUIDs (task triggers on `task_state_id` and `task_state_id` in step configs) to `status`. `downgrade()` restores `task_state_id`, creating default columns for a company that has none, and maps workflows back.
- `tests/test_task_status.py`.

### Changed
- `Task.task_state_id` is nullable. The `task_state` table, the FK and the `task_states.*` permissions stay until every consumer reads `status`.
- `TaskMove` and `TaskBulkReorder` take `status` instead of `task_state_id`. **Breaking** for backend-erp, which moves to `status` in the same re-pin.
- Workflow engine CREATE_TASK: `status` is optional and follows the assignment. A legacy `task_state_id` is still accepted and mapped through its kind, and an unresolved `{{param:...}}` is ignored. Position is computed within the status. The step output carries `status`.
- Seed templates `new-installation` (no board-column parameter) and `installation-provisioning` (fires on `status` changed to `DONE`, no parameter), gated on the `task.status` column.

## [1.33.0] - 2026-09-18

Insights v2 persistence (feature `insights-v2`, spec
`uplink-workspace/docs/superpowers/specs/2026-09-18-insights-v2-design.md` §5).

### Added
- `InsightChartType.LINE`.
- `InsightDashboard.default_time_range` (JSON, nullable): the dashboard's default TimeRange, `{"preset": ...}` or `{"from", "to"}`.
- `InsightChart.viz` (JSON, nullable): presentation settings `{"width": 1|2|3, "stacked": bool}`.
- Alembic revision `iv1_insights_v2` (parent `dc1_category_trim`, **new head**). It is hand-written: `ALTER TYPE insightcharttype ADD VALUE IF NOT EXISTS 'LINE'` inside `autocommit_block()`, the two `ADD COLUMN IF NOT EXISTS ... JSON`, and post-upgrade assertions. `downgrade()` drops the two columns and keeps the enum label (PG cannot drop labels). It refuses while any chart still uses `LINE`.
- `tests/test_insights_v2.py` (revision guardrails) and `tests/test_insight_schemas_v2.py` (schema pins).

### Changed
- `schemas/insight.py`: chart `spec` is now an opaque `Dict[str, Any]`. `viz` (`InsightChart*`) and `default_time_range` (`InsightDashboard*`) are opaque optional dicts. backend-erp owns query-spec v2 and validates all three on write.
- `ordering` moves from `InsightChartBase` into `InsightChartCreate` (`Optional[int] = None`, meaning the backend assigns it), `InsightChartUpdate` and `InsightChartOut` (`int`, required).

### Removed
- `InsightChartSpec` (the v1 `{entity, measure, dimension, filters}` shape). **Breaking** for any consumer that imports it: backend-erp replaces its v1 insights code in the same re-pin.

## [1.17.0] - 2026-07-19

### Removed
- **Client install fields (feature `client-install-field`, doc 31)**: `Client.installation_status` / `Client.installation_date` columns and the `InstallationStatus` enum are gone — a single stored per-client install state is ambiguous under multi-service and was a stale display cache; the truth is `client_service.install_state` (nc2a) + adoption attestation (ba1).
  - `ClientBase`/`ClientUpdate` (and thus `ClientCreate`/`ClientOut`) drop both fields.
  - `workflow_fields.py` client registry drops both entries.
  - `isp_seed.py` `new-installation` template v3: step s3 ("Mark client install scheduled") and edge s2→s3 removed — s2 (dispatch task) is terminal.

### Added
- `ClientOut.services_total` / `ClientOut.services_installed` (both `int`, default `0`) — read-only services-summary rollup COMPUTED by backend-erp's clients list/detail endpoints from `client_service` rows (`install_state='INSTALLED'` for the second count); never stored, never on Create/Update.
- Alembic revision `cf1_drop_client_install_fields` (parent `ba1_attested_adoption`, **new head**): data cleanup BEFORE the DDL — deletes installed `UPDATE_FIELD` workflow steps writing the dropped fields (edges rerouted predecessors→successors with dedupe; step executions keep their `step_name` snapshot via the c2e SET NULL FK) and clients insight charts using the `installation_status` dimension/filter — then drops both columns and the `installationstatus` PG enum type. Downgrade recreates structure only; data is not restorable.
- `tests/test_client_install_field_drop.py` guardrails (incl. single-head file scan).

## [1.16.0] - 2026-07-19

### Added
- **Attested Adoption (brownfield onboarding, doc 30)**:
  - `ClientService.adopted_at` / `adopted_by_user_id` (FK → `user`, ON DELETE SET NULL) / `adoption_note` columns, plus the `adopted_by` relationship and partial index `ix_client_service_adopted` (`company_id` WHERE `adopted_at IS NOT NULL`).
  - Read-only `ClientServiceOut` fields `adopted_at` / `adopted_by_user_id` / `adoption_note`, plus backend-computed `activation_evidence` (`'provisioned'` | `'attested'` | `None`; constants `ACTIVATION_EVIDENCE_*` in `models/isp.py` — not a DB column).
  - New schemas `ClientServiceAdoptIn` (note required non-empty, optional historical `installed_at`), `ClientServiceAdoptBulkItem`, `ClientServiceAdoptBulkIn` (1–500 items), `ClientServiceAdoptBulkRowResult`, `ClientServiceAdoptBulkOut`.
  - Permission `client_services.adopt` — ADMIN-only: excluded from the MANAGER auto-grants via `isp_seed.ADMIN_ONLY_PERMISSIONS` and `rbac_seed.MANAGER_EXCLUDED_PERMISSIONS` (subset-pinned by tests), granted to no ISP base role, no rbac_seed step-4 grant-copy source.
  - Alembic revision `ba1_attested_adoption` (parent `t2_grandfather_email_verified`): additive columns + FK + partial index + idempotent permission insert with global-ADMIN-only grant; total downgrade.
  - `tests/test_attested_adoption.py` guardrails.

### Notes
- `install_state` CHECK constraint and the `INSTALL_STATES` set are unchanged — adoption adds no state; it substitutes for job evidence only inside backend-erp's `_activation_ok` (a real SUCCEEDED job is checked first).

## [1.11.1] - 2026-07-07

### Added
- **ISP Insights (Cycle 4)** — tenant-defined analytics dashboards in `database_utils/models/isp.py`:
  - `InsightDashboard` (`insight_dashboard`): company-scoped, `UniqueConstraint(company_id, name)`; `Company` gains an `insight_dashboards` relationship.
  - `InsightChart` (`insight_chart`): scoped through its parent dashboard (no `company_id`); `chart_type` enum `InsightChartType` (`NUMBER`/`BAR`/`PIE`); `spec` JSON = `{entity, measure, dimension?, filters?}` (filters is a list of `{column, op, value}` clauses).
  - New Pydantic module `database_utils/schemas/insight.py`.
  - Alembic revision `c4a_insights_dashboards` (additive; creates both tables).

### Removed
- `Client.installation_address` column (Alembic revision `c4b_drop_installation_address`) — never populated separately from the billing `address`; clients now use their single `address`.

### Migrations
- New linear chain on the existing head: `c3b_device_categories` → `c4a_insights_dashboards` → `c4b_drop_installation_address`. New head: **`c4b_drop_installation_address`**.

## [0.7.0] - 2026-01-12

### Added
- **Comprehensive Audit Logging System**
  - New audit logging utilities in `database_utils/utils/audit_utils.py`
    - `log_create_operation()` - Log resource creation with full details
    - `log_update_operation()` - Log updates with before/after states
    - `log_delete_operation()` - Log deletions with preserved resource data
    - `log_custom_operation()` - Log custom actions not fitting CRUD pattern
  - New audit context dependencies in `database_utils/dependencies/audit.py`
    - `AuditContext` class for holding audit information
    - `get_client_ip()` function with proxy support (X-Forwarded-For, X-Real-IP)
    - `get_audit_context()` dependency for authenticated endpoints
    - `get_audit_context_optional()` dependency for optional authentication
  - Comprehensive documentation in `AUDIT_LOGGING.md` with integration guide and examples

### Changed
- Updated `audit_utils.py` from async to sync operations to match codebase architecture
- Enhanced `dependencies/__init__.py` to export new audit context dependencies
- Updated package description to highlight audit logging capabilities

### Fixed
- Improved JSON serialization in audit utilities to handle Pydantic models, datetime objects, and SQLAlchemy models

## [0.6.1] - 2025-XX-XX

### Previous release
- (Previous changes not documented here)

---

## Migration Guide to 0.7.0

### For Existing Projects

1. **Update the dependency** in your `requirements.txt`:
   ```
   git+https://github.com/pel19072/models-utils.git@main
   ```

2. **Import the new utilities** where needed:
   ```python
   from database_utils.dependencies.audit import get_client_ip
   from database_utils.utils.audit_utils import (
       log_create_operation,
       log_update_operation,
       log_delete_operation
   )
   ```

3. **Add audit logging to your endpoints**:
   - Add `request: Request` parameter to capture HTTP context
   - Capture IP address: `ip_address = get_client_ip(request)`
   - Add appropriate logging calls after CRUD operations

4. **No database migrations required** - The `audit_log` table already exists in the schema.

### Breaking Changes

None. This release is fully backward compatible.

### New Features Available

- Track all CRUD operations across your application
- Capture user context automatically from JWT tokens
- Record IP addresses with proxy support
- Store before/after states for updates
- Preserve deleted resource data for compliance

See `AUDIT_LOGGING.md` for detailed implementation guide and examples.
