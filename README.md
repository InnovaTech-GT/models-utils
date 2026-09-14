# models-utils (`database-utils`)

Shared data layer of the **Uplink** ISP platform — a pip-installable Python library
(package name `database-utils`, module `database_utils`, currently v1.13.0). It is
**not a running service**: it has no server, no port, and no entry point of its own.

models-utils owns everything that must be identical across the platform's backends:

- All **SQLAlchemy ORM models** for the single shared PostgreSQL database (auth, CRM, ISP, and workflow domains)
- All **Pydantic v2 request/response schemas** shared between services
- **Alembic migrations** plus idempotent seed scripts (RBAC, tiers, ISP catalog/templates)
- Cross-service **utilities**: JWT, password hashing, permission checks, audit logging, pagination, Guatemala timezone helpers, SSRF guard, OTEL helpers — and, crucially, the **workflow engine** and **provisioning resolution** logic
- The transactional **email service** (abstract + SMTP implementation) with Jinja2 HTML templates

It is the **schema authority** for Uplink: every database change flows through this
repo (model edit → Alembic autogenerate revision → SHA pin bump in the consuming
backends). Consumers — `backend-erp` (CRM API + provisioning worker), `auth-erp`,
and `cron-erp` — install it pinned by commit SHA:

```
database-utils @ git+https://github.com/InnovaTech-GT/models-utils.git@<commit-sha>
```

For the platform big picture (service map, request flow, release model), see the
root repo docs and [docs/connections.md](docs/connections.md).

## Tech Stack

- Python >= 3.11 (CI runs 3.12) — MIT license
- SQLAlchemy >= 2.0, Alembic >= 1.12, psycopg2-binary
- Pydantic >= 2.12.5, FastAPI >= 0.100 (dependency helpers)
- PyJWT, bcrypt, loguru, Jinja2, aiosmtplib, opentelemetry-api (API only)

## Quickstart

### As part of the full local stack (recommended)

From the **repo root** (`/home/rpellecer/cable`, one level above this repo):

```bash
docker compose up --build
```

The `migrate` compose service builds this repo's `Dockerfile` and runs
`alembic upgrade head` (+ seeds) against the local Postgres (`erp`/`erp`/`erp` @
`localhost:5432`) before the backends start.

### Standalone library development

```bash
pip install -e .                                  # editable install
pytest -v                                         # tests (in-memory SQLite; needs placeholder POSTGRES_* env)
alembic revision --autogenerate -m "description"  # generate a migration (needs a reachable DB)
```

## Documentation Wiki

| Page | Contents |
|---|---|
| [docs/README.md](docs/README.md) | Wiki index |
| [docs/overview.md](docs/overview.md) | Purpose, role in the platform, feature inventory |
| [docs/architecture.md](docs/architecture.md) | Internal layering, execution surfaces, import-direction constraint |
| [docs/connections.md](docs/connections.md) | How the other 4 services consume this library; env vars |
| [docs/external-dependencies.md](docs/external-dependencies.md) | Libraries and external systems |
| [docs/deployment-local.md](docs/deployment-local.md) | Local docker compose + standalone dev workflow |
| [docs/deployment-production.md](docs/deployment-production.md) | CI, prod migrations via GitHub Actions, release model |
| [docs/limitations.md](docs/limitations.md) | Known limitations, TODOs, tracked debt |
| [docs/auth-models.md](docs/auth-models.md) | Auth / tenancy / SaaS-billing models |
| [docs/crm-models.md](docs/crm-models.md) | CRM models (clients, orders, payments, tasks, integrations) |
| [docs/isp-models.md](docs/isp-models.md) | ISP vertical models (plans, services, inventory, playbooks, provisioning) |
| [docs/network-models.md](docs/network-models.md) | Network config + the company network graph (Cycles 5/7/10) |
| [docs/workflow-models.md](docs/workflow-models.md) | Workflow automation models |
| [docs/workflow-engine.md](docs/workflow-engine.md) | Workflow engine + provisioning resolution semantics |
| [docs/schemas.md](docs/schemas.md) | Pydantic schema modules |
| [docs/utilities.md](docs/utilities.md) | Shared utility modules |
| [docs/email-service.md](docs/email-service.md) | Transactional email service + templates |
| [docs/migrations.md](docs/migrations.md) | Alembic migration workflow, revision chains, seeds |

Agent-facing conventions live in [CLAUDE.md](CLAUDE.md); a file-level navigation
index lives in [CODEBASE_INDEX.md](CODEBASE_INDEX.md).

## Critical Rules

- **Never push directly to `main`** — feature branches + composed release PRs only (see [docs/deployment-production.md](docs/deployment-production.md)).
- Every model change **must** ship with an Alembic revision — CI blocks PRs that change `database_utils/models/**` without one.
- Additive schema changes are safe once consuming code is ready; **destructive changes** (drop/rename) require all consuming service code in production first.
