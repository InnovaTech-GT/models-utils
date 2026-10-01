# Changelog

All notable changes to the `database-utils` library will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
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
