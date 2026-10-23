# External Dependencies

## Python libraries (`setup.cfg` install_requires)

| Dependency | Version | Used for |
|---|---|---|
| `sqlalchemy` | >= 2.0, < 2.1 | ORM models, engine, sessions (the cap is load-bearing: 2.1 switches a bare `postgresql://` URL to psycopg v3, which is not shipped) |
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

Not installed by this package, imported lazily inside the one function that
needs it (6.9.0, [playbook-language.md](playbook-language.md)). Every consumer
that calls those functions pins it itself:

| Dependency | Imported in | Notes |
|---|---|---|
| `google-re2` (`re2`) | `playbook_render._render_regex` | Linear-time regex for rendered `expect_regex` / captures; in `requirements-dev.txt` for this repo's tests |
| `httpx` | `genieacs_nbi.nbi_client` | Sync client against the GenieACS NBI |

## External systems

| System | Relationship |
|---|---|
| **PostgreSQL** | The single shared database. Local: compose `postgres` service (`erp`/`erp`/`erp` @ `localhost:5432`). Production: Railway managed Postgres. |
| **GitHub Actions** | CI (`ci.yml`: migration guard, ruff advisory, pytest) and Railway DB migrations (`migrate.yml`: `alembic upgrade head` against the `development` / `production` environment's `DB_URL` secret on push to `develop` / `main`). |
| **Railway** | Only indirect — the `development` and `production` Postgres DBs whose `DB_URL`s the migration workflow uses. This library is never deployed as a Railway service. |
| **SMTP server** | Transactional email transport (host/credentials supplied by the consuming service; see [email-service.md](email-service.md)). |
| **Honeycomb** | Indirect — this repo ships OTEL API helpers only; consumers export the traces. |

| **GenieACS NBI** | 6.9.0: `utils/genieacs_nbi.py` holds the read-side helpers (`nbi_client`, `find_device_id`, `tenant_tag`). This library never dials it on its own; backend-erp (ACS administration) and the provisioning worker (tr069 driver, poller) call them. |

Not used by this repo: **Redis** (backend-erp concern) and device-management
targets other than the GenieACS NBI helpers above (device drivers live in the
provisioning worker).
