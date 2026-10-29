# CRM Models

## Description

SQLAlchemy ORM models for the CRM domain (`database_utils/models/crm.py`):
clients, orders + the payment ledger, invoices, custom fields, the task board,
integrations, and the uplink-mobile field records (uploaded files, collection
routes/visits, cash sessions, collection areas, task closeouts). The
per-company WhatsApp bot menu lives in its own module, `models/whatsapp_bot.py`
(see below).

## Goal

Provide a single shared definition for CRM database tables consumed primarily
by backend-erp (cron-erp reaches them only through backend-erp's HTTP API).

## Models (in `database_utils/models/crm.py`; table names in parens)

| Model | Purpose |
|-------|---------|
| `Client` (client) | Tenant's subscriber/customer. `payment_day` (SMALLINT NULL, CHECK 1..31, pd1) is the client's usual payment day of month. `code` (VARCHAR(16) NOT NULL, cc1) is a short per-company client id — the legacy id from `[LEGACY_ID:…]` where one existed, otherwise a random 6-char code (`utils/client_code.py`, DB default `client_code_generate()`); overrideable, unique per company case-insensitively (`uq_client_company_code`), format CHECK `ck_client_code_format`, and exposed to playbooks as `{{client.code}}`. `installation_status`/`installation_date` (and the `InstallationStatus` enum) were DROPPED by `cf1_drop_client_install_fields` — a single stored install state is ambiguous under multi-service; install truth is `client_service.install_state` ([isp-models.md](isp-models.md)) and the clients list/detail derive `services_total`/`services_installed` rollups in backend-erp |
| `Order` (order) | Customer order — enums include `OrderStatus`, `OrderType`, `PaymentStatus` |
| `OrderItem` (order_item) | Order line item; bills a `ServicePlan` (`service_plan_id`) with `unit_price_cents`/`product_name` snapshots. The legacy `product_id` column was dropped by `ld1_legacy_drop` |
| `Invoice` (invoice) | Customer invoice |
| `Payment` (payment) | **Cycle 1 payment ledger** — `PaymentKind`, `PaymentMethodType`; `idempotency_key` (pi1); `allocation_id` (rows of one multi-order collection share it) and `cash_session_id` (FK, the collector box it landed in) since mi2 |
| `UploadedFile` (uploaded_file) | Polymorphic evidence store (`UploadedFileOwnerType` TASK_CLOSEOUT/COLLECTION_VISIT/CASH_SESSION/PAYMENT); `idempotency_key` + partial unique per company since mi2 |
| `CashSession` (cash_session) | Collector cash box, `CashSessionStatus` OPEN/CLOSED/DEPOSITED (DEPOSITED since mi1) + SUBMITTED/REJECTED/APPROVED (cr1: collector submits, admin with `cash_sessions.review` approves/rejects; `submitted_at`, `reviewed_at`, `reviewed_by`, `review_note`); mi2 adds `opening_cents`, `deposited_at`/`deposited_cents`/`deposit_reference`, `closed_expected_cash_cents` (frozen at close), `reopen_count`, `movements` |
| `CashMovement` (cash_movement) | Cash top-up into a box; client-generated id = idempotency key (mi2) |
| `CollectionArea` (collection_area) | ca1: a collector's area drawn in the back-office (Configuración -> Áreas de cobro). `polygon` JSON ring of `[lat, lng]` pairs (open, at least 3 distinct points, validated by `schemas.collection_area.normalize_polygon`), `color` `#RRGGBB`, `name` unique per company, `collector_id` FK user SET NULL with at most one area per collector. CHECKs on color and polygon live in the revision only |
| `CollectionRoute` (collection_route) | A collector's day route (rs1): `route_date`, `status` `CollectionRouteStatus` OPEN/CLOSED, `collector_id` FK user RESTRICT; `stops` → `RouteStop` |
| `RouteStop` (route_stop) | One client on a route: `sequence`, `status` `RouteStopStatus` PENDING/VISITED/NO_CONTACT; `visits` → `CollectionVisit` |
| `CollectionVisit` (collection_visit) | Outcome of a stop: `outcome` `CollectionVisitOutcome` PAID/PARTIAL/NO_CONTACT, `visit_code` `CollectionVisitCode` (camelCase values `noOneHome`/`promise`/`refused`/`complaint`/`moved`, matching uplink-mobile verbatim — mapped with `values_callable`), `promise_date`, `note`, optional `payment_id` / `signature_file_id` |
| `TaskCloseout` (task_closeout) | One-to-one technician closeout of a `Task` (tc1): checklist state, serial number / device match, GPS fix, signer |
| `CustomFieldDefinition` / `ClientCustomFieldValue` | Dynamic per-tenant client fields. Also a **provisioning input**: each value is emitted as the playbook variable `client.<field_key>` (doc 33 follow-up), so a subscriber's static IP or VLAN can be templated into device config. Values are stored as strings and coerced by `field_type` at resolution |
| `Task` (task) | Work item; `status` PENDING/ASSIGNED/IN_PROGRESS/DONE (`TASK_STATUSES`, `ck_task_status`), PENDING and ASSIGNED derived from the technician assignment (`utils/task_status.py`); `route_sequence` = order in the technician's route for `scheduled_date` (dispatch ETL, `dr1_task_route_sequence`); `latitude`/`longitude` = the task's own reference point, both or neither (`ck_task_location`, `tl1_task_location`, doc 46; NULL = derived in backend-erp `utils/tasks.reference_point`; output-only on `TaskOut`, inputs on the backend's `TaskCreateIn`/`TaskUpdateIn`); `onu_auto_assigned` BOOLEAN NOT NULL false = the server picked `inventory_item_id` (custody-first / warehouse FIFO, backend-erp `services/onu_assignment.py`; auto picks are re-evaluated on technician change, manual ones never; output-only on `TaskOut`; `oa1_task_onu_auto_assigned`, doc 45); `started_at`/`completed_at` + `step_progress` JSON (per-step field progress, mi2); `materials` → `TaskMaterial`; `job_kind` `TaskJobKind` INSTALL/FAULT/CHANGE/REMOVE/SUSPEND/MAINTENANCE/RELOCATION (mi1); assignees via the `TaskAssignee` model (`task_assignee`, M2M with a `role`); `TaskLinkedObjectType` CLIENT/ORDER/CLIENT_SERVICE/INVENTORY_ITEM (RECURRING_ORDER removed by `ld1`) |
| `TaskMaterial` (task_material) | Materials reported on a task, one row per device type (`uq_task_material_task_type`); `quantity` in `device_type.unit`, `consumed_quantity`/`shortfall`/`consumed_at` stamped when the task becomes DONE (mi2) |
| `TaskTemplate` (task_template) | Task blueprint |
| `Integration` (integration) | External API connection — `IntegrationAuthType` NONE/API_KEY/BEARER_TOKEN/BASIC_AUTH; `enabled` (bool, default true — disabled = kept but refused by consumers) and `provider` (nullable tag, `WHATSAPP_BUSINESS` or `RECURRENTE` (pl1); schemas type it as a `Literal`) since fg1 |

## WhatsApp bot menu (`models/whatsapp_bot.py`)

| Model | Purpose |
|-------|---------|
| `WhatsAppBotConfig` (whatsapp_bot_config) | wb1: the WhatsApp bot (Nova) menu, one row per company (`uq_whatsapp_bot_config_company`). `enabled` (false = the bot uses its built-in menu), `welcome_text`, `menu_button_text` (default `Ver opciones`), `options` JSON validated on every write by `schemas.whatsapp_bot` ([schemas.md](schemas.md)); the gateway reads it through backend-erp's whatsapp-config endpoint |
| `PaymentLink` (payment_link) | pl1: one online payment link asked of a gateway (today only Recurrente) for one order (`models/payment_link.py`). `status` CREATING/PENDING/PAID/EXPIRED/REVIEW/FAILED (`PaymentLinkStatus`; `ACTIVE_PAYMENT_LINK_STATUSES` = CREATING, PENDING), `amount_cents`, `currency`, `payment_methods`, `expires_at`, `checkout_id`/`checkout_url`/`live_mode`, `last_checked_at`/`checks`, `intent_id` (unique), `payment_id` → payment, `paid_at`, `review_reason`, `wa_integration_id` (FK integration SET NULL) / `wa_to` (VARCHAR(32), the client's WhatsApp number, PII: never in `PaymentLinkOut` nor the workflow engine) / `notified_at` (doc 59 §6: where and whether the client was told the payment landed). One live link per order. Never the source of truth for "paid": backend-erp confirms with the gateway and records through `PaymentService` |

## Connections to Other Components

- **backend-erp**: primary consumer of all CRM models
- **cron-erp**: calls backend-erp's `/recurring-orders/process-all-due` URLs (billing runs off `ClientService`)
- **ISP models** ([isp-models.md](isp-models.md)): `ServicePlan` replaced the
  legacy `Product`; `ClientService` carries the recurring billing that
  `RecurringOrder` used to (both legacy tables dropped by `ld1_legacy_drop`)
- **Workflow engine** ([workflow-engine.md](workflow-engine.md)): fires on CRM
  entity events; `CREATE_ORDER`/`CREATE_TASK` steps create these rows;
  `HTTP_REQUEST` steps use `Integration` credentials
- **CRM schemas** ([schemas.md](schemas.md)): Pydantic representations

## Key Implementation Details

- All models: UUID v4 primary key + `created_at`/`updated_at` timestamps
- `ld1_legacy_drop` (4.0.0) removed `Product`, `RecurringOrder`/`RecurringOrderItem`
  and `TaskState` (tables + `order.recurring_order_id`, `order_item.product_id`,
  `task.task_state_id`); the `RecurrenceEnum`/`RecurringOrderStatus` enums stay
  (`ClientService` uses them)
- Enums: `OrderStatus`, `OrderType`, `PaymentStatus`, `PaymentKind`,
  `PaymentMethodType`, `RecurrenceEnum`, `RecurringOrderStatus`,
  `ServiceAvailability`, `CustomFieldType` (TEXT/NUMBER/EMAIL/PHONE/URL/DATE/BOOLEAN),
  `TaskJobKind` (INSTALL/FAULT/CHANGE/REMOVE/SUSPEND/MAINTENANCE/RELOCATION/RECONNECT — RECONNECT since pa1, doc 56), `TaskLinkedObjectType` (CLIENT/ORDER/CLIENT_SERVICE/INVENTORY_ITEM),
  `IntegrationAuthType`, `UploadedFileOwnerType`, `UploadedFileKind` (PHOTO/SIGNATURE),
  `CollectionRouteStatus`, `RouteStopStatus`, `CollectionVisitOutcome`,
  `CollectionVisitCode`, `CashSessionStatus`
  (`InstallationStatus` removed by `cf1_drop_client_install_fields`)
- Task assignees: many-to-many with `User` via the `task_assignee` table

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
