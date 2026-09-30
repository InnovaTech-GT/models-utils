# CRM Models

## Description

SQLAlchemy ORM models for the CRM domain (`database_utils/models/crm.py`):
clients, orders + the payment ledger, invoices, legacy catalog/recurring
billing, custom fields, the task board, and integrations.

## Goal

Provide a single shared definition for CRM database tables consumed primarily
by backend-erp (and cron-erp for recurring orders).

## Models (in `database_utils/models/crm.py`; table names in parens)

| Model | Purpose |
|-------|---------|
| `Client` (client) | Tenant's subscriber/customer. `installation_status`/`installation_date` (and the `InstallationStatus` enum) were DROPPED by `cf1_drop_client_install_fields` — a single stored install state is ambiguous under multi-service; install truth is `client_service.install_state` ([isp-models.md](isp-models.md)) and the clients list/detail derive `services_total`/`services_installed` rollups in backend-erp |
| `Product` (product) | **Legacy catalog item** — absorbed by the Cycle 2 catalog merge (`c2a`) into `ServicePlan` with hybrid `CatalogKind`; bridge-less legacy products are treated as SERVICE. Retained during the rollback window |
| `Order` (order) | Customer order — enums include `OrderStatus`, `OrderType`, `PaymentStatus` |
| `OrderItem` (order_item) | Order line item (`product_id` deprecated but still honored) |
| `RecurringOrder` (recurring_order) + `RecurringOrderItem` | **Legacy billing engine** (`RecurrenceEnum`) — `ClientService` absorbed its billing in Cycle 2 (`c2b`) but still dual-writes here during the rollback window; consumed by cron-erp |
| `Invoice` (invoice) | Customer invoice |
| `Payment` (payment) | **Cycle 1 payment ledger** — `PaymentKind`, `PaymentMethodType`; `idempotency_key` (pi1); `allocation_id` (rows of one multi-order collection share it) and `cash_session_id` (FK, the collector box it landed in) since mi2 |
| `UploadedFile` (uploaded_file) | Polymorphic evidence store (`UploadedFileOwnerType` TASK_CLOSEOUT/COLLECTION_VISIT/CASH_SESSION/PAYMENT); `idempotency_key` + partial unique per company since mi2 |
| `CashSession` (cash_session) | Collector cash box, `CashSessionStatus` OPEN/CLOSED/DEPOSITED (DEPOSITED since mi1); mi2 adds `opening_cents`, `deposited_at`/`deposited_cents`/`deposit_reference`, `closed_expected_cash_cents` (frozen at close), `reopen_count`, `movements` |
| `CashMovement` (cash_movement) | Cash top-up into a box; client-generated id = idempotency key (mi2) |
| `CollectionRoute` / `RouteStop` / `CollectionVisit` / `TaskCloseout` | uplink-mobile route/visit/closeout records (rs1/tc1) |
| `CustomFieldDefinition` / `ClientCustomFieldValue` | Dynamic per-tenant client fields. Also a **provisioning input**: each value is emitted as the playbook variable `client.<field_key>` (doc 33 follow-up), so a subscriber's static IP or VLAN can be templated into device config. Values are stored as strings and coerced by `field_type` at resolution |
| `TaskState` (task_state) | Legacy board column (`TaskStateColor`). Superseded by `task.status` since `ts1_task_status`, dropped in a later revision |
| `Task` (task) | Work item; `status` PENDING/ASSIGNED/IN_PROGRESS/DONE (`TASK_STATUSES`, `ck_task_status`), PENDING and ASSIGNED derived from the technician assignment (`utils/task_status.py`); `route_sequence` = order in the technician's route for `scheduled_date` (dispatch ETL, `dr1_task_route_sequence`); `started_at`/`completed_at` + `step_progress` JSON (per-step field progress, mi2); `materials` → `TaskMaterial`; `job_kind` `TaskJobKind` INSTALL/FAULT/CHANGE/REMOVE/SUSPEND/MAINTENANCE/RELOCATION (mi1); assignees via the `TaskAssignee` model (`task_assignee`, M2M with a `role`); `TaskLinkedObjectType` CLIENT/ORDER/RECURRING_ORDER |
| `TaskMaterial` (task_material) | Materials reported on a task, one row per device type (`uq_task_material_task_type`); `quantity` in `device_type.unit`, `consumed_quantity`/`shortfall`/`consumed_at` stamped when the task becomes DONE (mi2) |
| `TaskTemplate` (task_template) | Task blueprint |
| `Integration` (integration) | External API connection — `IntegrationAuthType` NONE/API_KEY/BEARER_TOKEN/BASIC_AUTH; `enabled` (bool, default true — disabled = kept but refused by consumers) and `provider` (nullable tag, `WHATSAPP_BUSINESS` only; schemas type it as a `Literal`) since fg1 |

## Connections to Other Components

- **backend-erp**: primary consumer of all CRM models
- **cron-erp**: consumes `RecurringOrder` for nightly recurring order generation
- **ISP models** ([isp-models.md](isp-models.md)): `ServicePlan` superseded
  `Product`; `ClientService` supersedes `RecurringOrder` billing (dual-write
  link retained)
- **Workflow engine** ([workflow-engine.md](workflow-engine.md)): fires on CRM
  entity events; `CREATE_ORDER`/`CREATE_TASK` steps create these rows;
  `HTTP_REQUEST` steps use `Integration` credentials
- **CRM schemas** ([schemas.md](schemas.md)): Pydantic representations

## Key Implementation Details

- All models: UUID v4 primary key + `created_at`/`updated_at` timestamps
- Cycle 2 dual-write: `ClientService` still writes legacy `recurring_order`
  rows until the rollback window closes (see
  [limitations.md](limitations.md))
- Enums: `OrderStatus`, `OrderType`, `PaymentStatus`, `PaymentKind`,
  `PaymentMethodType`, `RecurrenceEnum`, `ServiceAvailability`,
  `TaskStateColor`, `TaskLinkedObjectType`, `IntegrationAuthType`
  (`InstallationStatus` removed by `cf1_drop_client_install_fields`)
- Task assignees: many-to-many with `User` through the `TaskAssignee` model (table `task_assignee`: `task_id`, `user_id`, `role` TECHNICIAN/COLLECTOR or legacy NULL)

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
