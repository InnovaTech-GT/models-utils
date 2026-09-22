# Architecture

Diagram: [architecture.excalidraw](architecture.excalidraw)

## Layering

Import direction is strictly downward:

```
services (email)   middleware   dependencies
        \              |             /
         utils (workflow engine, provisioning resolution,
                jwt, permissions, audit, ssrf, ...)
                       |
        schemas (Pydantic v2)    models (SQLAlchemy 2.0, Base)
                       \              /
                 database.py (engine / SessionLocal)
```

- `database.py` builds the engine at import time (lazy connect) from
  `DATABASE_URL`, `DB_URL`, or composed `POSTGRES_*` env vars — and **raises at
  import if none are set**. `pool_pre_ping` and `pool_recycle=3600` are enabled.
  Exports `SessionLocal` and the declarative `Base`.
- `models/__init__.py` registers all four domain modules (`auth`, `crm`, `isp`,
  `workflow`) — `alembic/env.py` imports it wholesale so autogenerate sees every
  table.
- `schemas/__init__.py` star-imports all 42 schema modules and runs
  `model_rebuild()` to resolve circular Order/RecurringOrder references.
  Cycle 4/5 additions: `insight`, `acs_registration`, `device_credential`,
  `network_access`, `provisioning_settings`.
- `dependencies/` (FastAPI `get_db`, audit context) and
  `middleware/` (request logging) sit on top for consumers to wire in.
- `services/email_service.py` is the highest layer (uses utils + templates).

## Two execution surfaces

The library has **no entry point of its own**. It runs in exactly two ways:

1. **As a library** — imported by the FastAPI apps (`auth-erp`, `backend-erp`,
   including backend-erp's `provision-worker` process) and by `cron-erp`.
2. **As a migration runner** — `alembic upgrade head`:
   - locally via the repo-root compose `migrate` service, which builds this
     repo's `Dockerfile` (python:3.12-slim, `pip install .`,
     CMD `alembic upgrade head`);
   - in production via GitHub Actions on push to `main`.
   After upgrading, `alembic/env.py` runs `_run_seeds(connection)` (RBAC, tier,
   and ISP seeds — all idempotent). The Dockerfile exists solely for the compose
   one-shot; production service images never build it.

## The import-direction constraint

A key architectural rule, documented in the `provisioning_resolution.py`
docstring: **backend-erp imports models-utils, never the reverse.** Any logic
the workflow engine needs must therefore live in this repo. This is why
business logic has migrated *down* into this "models" library — most notably
`provisioning_resolution.py`, moved here from backend-erp in Cycle 3 so the
engine's `ENQUEUE_PROVISIONING` step can resolve playbooks — joined in Cycle 10
by `network_graph.py` (the plant traversal) and `provisioning_runs.py`
(multi-device runs) for exactly the same reason. See
[workflow-engine.md](workflow-engine.md).

## Startup flow (in a consumer)

1. Consumer process imports `database_utils` → `database.py` reads env and
   constructs the engine (no connection yet).
2. FastAPI app wires `get_db` (per-request session with rollback + close),
   the `get_client_ip` audit helper, and optionally `LoggingMiddleware`
   (request-ID + JWT-context + duration logging).
3. CRUD routers call the workflow engine after mutations via
   `asyncio.create_task(check_workflow_triggers(...))`.
4. OTEL: this library ships `opentelemetry-api` helpers only
   (`telemetry_utils.get_tracer`, `set_request_span_attributes`); the SDK and
   Honeycomb exporter are configured by the consuming services.
