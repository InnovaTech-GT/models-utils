# Production Deployment

models-utils is never deployed as a service. "Deploying" it means two things:

1. **Publishing a commit SHA** that the backends pin in their `requirements.txt`
2. **Migrating the production database** — done automatically by GitHub Actions

## GitHub Actions

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | PRs to `develop`/`main`, pushes to `main` | **Migration guard** (fails if `database_utils/models/**` changed without an `alembic/versions/**` file — protects the path-filtered prod migrate), ruff (advisory, `continue-on-error`), pytest on Python 3.12 |
| `.github/workflows/migrate.yml` | Push to `main` with an `alembic/**` path filter (widened to include `env.py` and seeds) | Runs `alembic upgrade head` against the production `secrets.DB_URL` in the `production` GitHub environment |

**Rule:** every seed change ships with a (possibly no-op) Alembic revision, so
the path-filtered prod migration actually fires.

## Branch & release model

Feature branches are created **from `main`** (never from `develop`), named
`{type}/{feature-id}/models-{description}`.

`develop` is a **composed release branch**: each cycle it is
`git reset --hard main`, then selected verified feature branches are merged
`--no-ff` and force-pushed (the `erp-release` skill orchestrates this). Never
commit to `develop` directly.

Release flow for a schema change:

1. Feature branch from `main` → model edit + autogen revision + `setup.cfg`
   version bump → push
2. Backends (`backend-erp`, `auth-erp`) pin the feature-branch commit SHA in
   `requirements.txt`
3. Compose into `develop`; backends re-pin to the composed `develop` HEAD SHA
4. E2E on the local docker compose stack (checkout `develop` per service,
   `docker compose up --build` — the `migrate` service applies the head)
5. One `develop` → `main` PR per service; merging the models-utils PR triggers
   `migrate.yml` against the production DB; merging the service PRs triggers
   their Railway deploys
6. Delete feature branches

After merge to `main`, the previously pinned feature-branch SHA remains valid
(it is part of main's history).

## Safety rules

- **Additive** changes (new columns/tables): safe once consuming service code
  is ready.
- **Destructive** changes (drop/rename): all consuming service code must be in
  production FIRST.
- Not every migration is reversible — `c1e_install_actions` uses
  `ALTER TYPE ... ADD VALUE` and has no downgrade (see
  [migrations.md](migrations.md) and [limitations.md](limitations.md)).
- Parallel schema features get **separate** models-utils branches with each
  backend pinned to its own SHA — never combine unrelated schema changes.

## Current state

`develop` = the Cycles 1–3 composition (`6843506`, "release: compose Cycles
1-3"); both backends pin that SHA, promotion PRs to `main` pending.
