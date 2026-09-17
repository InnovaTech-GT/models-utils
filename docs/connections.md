# Connections to Other Services

models-utils is a library, so its "connections" are import relationships and
the shared database — it makes no network calls except SMTP (email service) and
the DB connection itself.

## Platform context

```
Browser ──> frontend-erp (Next.js proxies) ──> auth-erp (/api/auth/v1, /api/admin/v1)
                                          └──> backend-erp (/api/crm/v1)
auth-erp ──┐
backend-erp ─┴──> one shared PostgreSQL — schema owned by models-utils/Alembic
backend-erp routers ──write ProvisioningJob rows──> provision-worker executes playbooks
cron-erp (nightly) ──HTTP──> backend-erp + auth-erp billing endpoints
```

auth-erp and backend-erp never call each other over HTTP; their coupling is the
shared DB, these shared models, and the shared JWT `SECRET_KEY` (auth-erp issues
tokens, backend-erp validates them via `jwt_utils`).

## Consumers

| Consumer | How | What it uses |
|---|---|---|
| `backend-erp` | `requirements.txt` pin `database-utils @ git+https://github.com/InnovaTech-GT/models-utils.git@<sha>` | Models, schemas, `get_db`, permission/audit utils, **workflow engine** (called after CRUD mutations), provisioning models + resolution (worker + manual provision endpoint) |
| `auth-erp` | same SHA-pinned dependency | Auth models/schemas, `jwt_utils`, **email service + templates**, invitation/verification/reset tokens, SaaS billing models |
| `cron-erp` | pip dependency | RecurringOrder models for recurring order generation |
| `frontend-erp` | none (indirect) | Consumes JSON shaped by these Pydantic schemas via backend proxies |
| repo-root `docker-compose.yml` | `migrate` service builds this repo's `Dockerfile` | Applies Alembic head + seeds to the local Postgres before backends start |

Both backends are currently pinned to `6843506420f6cd1705932682ce3cb8bfee6482ea`
(= `develop` HEAD, the Cycles 1–3 release composition). During feature work each
backend pins the feature-branch SHA; at release composition both re-pin to the
composed `develop` HEAD. See [deployment-production.md](deployment-production.md).

## Environment variables read by this package

| Variable(s) | Where | Purpose |
|---|---|---|
| `DATABASE_URL` / `DB_URL` / `POSTGRES_USER`+`POSTGRES_PASSWORD`+`POSTGRES_HOST`+`POSTGRES_PORT`+`POSTGRES_DB` | `database.py`, `alembic/env.py` | DB connection (first match wins; import fails if none set) |
| `SECRET_KEY`, `ENVIRONMENT`, `ACCESS_TOKEN_EXPIRE` (minutes, default 1440), `REFRESH_TOKEN_EXPIRE` | `utils/jwt_utils.py` | JWT signing/validation; fails fast if `SECRET_KEY` unset when `ENVIRONMENT=production`, dev fallback otherwise |
| `EMAIL_PROVIDER`, `SMTP_USE_TLS` (+ SMTP host/credentials supplied by consumers) | `services/email_service.py` | Email provider selection and transport |

The package does **not** read `AUTH_URL`/`CRM_URL` and does **not** use Redis —
those belong to the services.
