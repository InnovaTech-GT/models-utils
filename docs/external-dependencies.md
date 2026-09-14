# External Dependencies

## Python libraries (`setup.cfg` install_requires)

| Dependency | Version | Used for |
|---|---|---|
| `sqlalchemy` | >= 2.0 | ORM models, engine, sessions |
| `alembic` | >= 1.12 | Schema migrations |
| `psycopg2-binary` | — | PostgreSQL driver |
| `pydantic` | >= 2.12.5 | Request/response schemas |
| `fastapi` | >= 0.100.0 | Dependency helpers (`get_db`, audit context), exception handlers, middleware |
| `PyJWT` | >= 2.8.0 | HS256 token create/decode (`jwt_utils`) |
| `bcrypt` | >= 4.2.0 | Password hashing |
| `loguru` | >= 0.7.3 | Structured JSON logging (`logging_utils`) |
| `python-dotenv` | >= 1.0 | Env loading |
| `email-validator` | >= 2.0.0 | Email field validation |
| `opentelemetry-api` | >= 1.20.0 | **API only** — tracer helpers; the SDK/exporter (Honeycomb) is configured by consuming services |
| `Jinja2` | >= 3.1 | Email HTML templates |
| `aiosmtplib` | >= 3.0 | Async SMTP email delivery |

## External systems

| System | Relationship |
|---|---|
| **PostgreSQL** | The single shared database. Local: compose `postgres` service (`erp`/`erp`/`erp` @ `localhost:5432`). Production: Railway managed Postgres. |
| **GitHub Actions** | CI (`ci.yml`: migration guard, ruff advisory, pytest) and production migrations (`migrate.yml`: `alembic upgrade head` against the prod `DB_URL` secret on push to `main`). |
| **Railway** | Only indirect — the production DB whose `DB_URL` the migration workflow uses. This library is never deployed as a Railway service. |
| **SMTP server** | Transactional email transport (host/credentials supplied by the consuming service; see [email-service.md](email-service.md)). |
| **Honeycomb** | Indirect — this repo ships OTEL API helpers only; consumers export the traces. |

Not used by this repo: **Redis** (backend-erp concern), **GenieACS** and other
device-management targets (the network config layer, docs 21/23 in the platform
ADRs, is design-only and not implemented here).
