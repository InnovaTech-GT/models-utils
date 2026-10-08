# Overview

## What models-utils is

models-utils (pip package **`database-utils`**, Python module **`database_utils`**,
v6.1.0 with ri1 (inventory intake), 4.5.1 on `main`) is the **shared data layer** of the Uplink ISP platform. It is a
pip-installable Python library — **not a running service**. There is no server,
no port, and no entry point; it executes only inside its consumers and as an
Alembic migration runner.

The three-way naming mismatch (repo `models-utils` / package `database-utils` /
module `database_utils`) is historical.

## Role in the platform

Uplink is a multi-tenant vertical SaaS for cable & internet providers. Its two
backends (`backend-erp` at `/api/crm/v1`, `auth-erp` at `/api/auth/v1` and
`/api/admin/v1`) share **one PostgreSQL database** and never call each other over
HTTP — their coupling is this library: shared models, shared schemas, and a
shared JWT `SECRET_KEY` validated through `jwt_utils`.

models-utils is the **schema authority**: every DB change is a model edit here
plus an Alembic autogenerate revision, followed by a SHA pin bump in the
consuming backends. Its CI blocks PRs that change models without a revision.

## What it owns

1. **SQLAlchemy ORM models** for all four domains — auth/tenancy/SaaS-billing
   ([auth-models.md](auth-models.md)), CRM ([crm-models.md](crm-models.md)),
   ISP vertical ([isp-models.md](isp-models.md)), and workflow automation
   ([workflow-models.md](workflow-models.md)). The ISP module also holds the
   Cycle 4 **insights** models (`InsightDashboard`, `InsightChart`; v2 adds `LINE`, `default_time_range` and `viz`) and the
   Cycle 5 **network-config** models (GenieACS/TR-069), the Cycle 7
   **core-config** columns (CORE/EDGE tiers, mgmt surface, install state) and the
   Cycle 10 **company network graph** — the `inventory_item` tree, the playbook
   binding tables and `ProvisioningRun`, replacing the deleted `Topology`
   entities, plus doc 40's port-level topology (`InventoryItemPort`,
   `NetworkLink`) (see [network-models.md](network-models.md)). All models use
   UUID v4 primary keys and `created_at`/`updated_at` timestamps.
2. **Pydantic v2 schemas** — 37 modules shared between services
   ([schemas.md](schemas.md)).
3. **Alembic migrations + idempotent seeds** — 101 revisions (head
   `ri1_inventory_received_at`; prod is at `vw1_viewer_no_credential_read`); RBAC, tier, and ISP catalog/template seeds run
   automatically after upgrade ([migrations.md](migrations.md)).
4. **Cross-service utilities** — JWT, password hashing, permission checks,
   audit logging, pagination, Guatemala timezone helpers, SSRF guard, OTEL
   helpers, and AES-256-GCM envelope encryption (`crypto.py`, for device
   credentials) ([utilities.md](utilities.md)) — and, crucially, the **workflow
   engine** and **provisioning resolution** logic
   ([workflow-engine.md](workflow-engine.md)).
5. **Transactional email service** — abstract interface + SMTP implementation
   with 13 Jinja2 HTML templates (two base layouts + es/en pairs + legacy single-locale ones) ([email-service.md](email-service.md)).
6. **FastAPI plumbing** — `get_db` session dependency, audit context
   dependencies, request-logging ASGI middleware.

## Consumers

| Service | Relationship |
|---|---|
| `backend-erp` (CRM API + provisioning worker) | pip pin by commit SHA |
| `auth-erp` | pip pin by commit SHA |
| `cron-erp` | pip dependency (billing enums) |
| `frontend-erp` | none directly — consumes JSON shaped by these schemas via backend proxies |
| repo-root `docker-compose.yml` | builds this repo's Dockerfile as the one-shot `migrate` service |

Details in [connections.md](connections.md).

## Tests

`tests/` holds 65 files, **758 tests** — 737 SQLite + 21 in `tests/pg`, which run only with `PG_TEST_URL` (`pytest.ini` sets `asyncio_mode = auto`),
running against in-memory SQLite so CI needs only placeholder `POSTGRES_*` env.
`conftest.py` provides the shared `db` + `plant` fixtures — a real in-memory
network graph rather than fakes, because resolution now runs recursive CTEs and
two binding lookups. Coverage: workflow engine, the network graph (columns,
traversal ordering, cycle/depth guards, cross-tenant isolation), playbook
binding precedence, provisioning resolution, provisioning runs (incl. the
Postgres-only dedupe / run-lock / SKIP LOCKED repair races), port topology and
playbook `computed` expressions, ng1/ng2 revision
guardrails, passive seed convergence, the service lifecycle machine,
ENQUEUE_PROVISIONING dedupe, core-config model↔migration constant parity, SSRF,
JWT expiry, token utils, email schemas/service/templates/SMTP, permission UUIDs,
smoke.
