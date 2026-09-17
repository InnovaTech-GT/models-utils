# Changelog

All notable changes to the `database-utils` library will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
