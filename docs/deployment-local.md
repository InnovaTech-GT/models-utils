# Local Development & Deployment

There is no "running" models-utils — locally it exists in two forms: the
`migrate` one-shot in the docker compose stack, and an editable pip install for
library development.

## Full local stack (docker compose)

From the **repo root** (`/home/rpellecer/cable`, one level above this repo):

```bash
docker compose up --build     # postgres + redis + migrate + auth + backend + frontend
./scripts/load-prod-data.sh   # optional: copy production rows into the local schema
docker compose down           # stop
docker compose down -v        # stop + wipe the local DB volume
```

- The **`migrate` compose service** builds this repo's `Dockerfile`
  (python:3.12-slim, `pip install .`, CMD `alembic upgrade head`) and applies
  the Alembic head **plus seeds** to the local Postgres (`erp`/`erp`/`erp` @
  `localhost:5432`) before the backends start.
- The `migrate` service uses this repo's **working tree**, while the backends
  install their pinned git SHA from `requirements.txt`. Keep the working tree
  at the pinned SHA (or rebuild) to stay consistent.
- Schema vs data: `migrate` creates tables/relationships; `load-prod-data.sh`
  copies production rows (data-only, excludes `alembic_version`).

The former Railway *development* environment was decommissioned — the local
compose stack is the only dev/staging environment.

## Standalone library development

```bash
pip install -e .   # editable install into your venv
pytest -v          # ~66 tests on in-memory SQLite
```

Tests need only **placeholder** `POSTGRES_*` env vars (`database.py` raises at
import if no DB env is set at all, but tests never connect to Postgres).

## Generating a migration

```bash
alembic revision --autogenerate -m "description"
```

Requires a reachable database in env (`DATABASE_URL`, `DB_URL`, or
`POSTGRES_*`) — with the local stack up, the compose Postgres at
`localhost:5432` works. Review the generated revision before committing; the
full ordered workflow (branch → revision → pin bump → compose) is in
[migrations.md](migrations.md) and the repo [CLAUDE.md](../CLAUDE.md)
(erp-migration skill).

## CI parity

GitHub Actions `ci.yml` runs on PRs to develop/main and pushes to main:

1. **Migration guard** — fails the PR if `database_utils/models/**` changed
   without a corresponding `alembic/versions/**` file
2. ruff (advisory only, `continue-on-error: true`)
3. pytest on Python 3.12

Run `pytest -v` locally before pushing to match.
