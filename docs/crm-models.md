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
| `Payment` (payment) | **Cycle 1 payment ledger** — `PaymentKind`, `PaymentMethodType` |
| `CustomFieldDefinition` / `ClientCustomFieldValue` | Dynamic per-tenant client fields. Also a **provisioning input**: each value is emitted as the playbook variable `client.<field_key>` (doc 33 follow-up), so a subscriber's static IP or VLAN can be templated into device config. Values are stored as strings and coerced by `field_type` at resolution |
| `TaskState` (task_state) | Kanban column (`TaskStateColor`) |
| `Task` (task) | Work item; assignees via `task_assignee` M2M; `TaskLinkedObjectType` CLIENT/ORDER/RECURRING_ORDER |
| `TaskTemplate` (task_template) | Task blueprint |
| `Integration` (integration) | External API connection — `IntegrationAuthType` NONE/API_KEY/BEARER_TOKEN/BASIC_AUTH |

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
- Task assignees: many-to-many with `User` via the `task_assignee` table

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
