# Production Deployment

models-utils is never deployed as a service. "Deploying" it means two things:

1. **Publishing a commit SHA** that the backends pin in their `requirements.txt`
2. **Migrating the production database** — done automatically by GitHub Actions

## GitHub Actions

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | PRs to `develop`/`main`, pushes to `main` | **Migration guard** (fails if `database_utils/models/**` changed without an `alembic/versions/**` file — protects the path-filtered prod migrate), ruff (advisory, `continue-on-error`), pytest on Python 3.12; job **`pg`**: a `postgres:16` service, `alembic upgrade head`, then `pytest -m pg tests/pg` (deferred triggers, composite FKs, migration refusals, provisioning-run races) |
| `.github/workflows/migrate.yml` | Push to `main` (or `develop`, for the Railway dev DB) with an `alembic/**` path filter (widened to include `env.py` and seeds), or `workflow_dispatch` | Runs `alembic upgrade head` against `secrets.DB_URL` of the `production` (or `development`) GitHub environment |

**Rule:** every seed change ships with a (possibly no-op) Alembic revision, so
the path-filtered prod migration actually fires.

## Branch & release model

Feature branches are created **from `main`** for a standalone feature, or
**from `develop`** when the work builds on cycles already on `develop` but not
yet released (e.g. port-topology C8a on top of pt1). Naming:
`{type}/{feature-id}/models-{description}`.

`develop` is a **long-lived branch that accumulates verified features by
merge** (`git merge --no-ff`, the `erp-release` skill orchestrates this). It is
**never reset to `main` and never force-pushed** — resetting destroys every
composed-but-unreleased commit. To drop a feature from `develop`,
`git revert -m 1 <its-merge-commit>`. Pushing `develop` with an `alembic/**`
change migrates the Railway `development` Postgres.

Release flow for a schema change:

1. Feature branch → model edit + autogen revision + `setup.cfg` version bump →
   push
2. Backends (`backend-erp`, `auth-erp`) pin the feature-branch commit SHA in
   `requirements.txt`
3. Merge into `develop` and push **models-utils first**; wait for the
   "Alembic Migrate" run against the Railway `development` Postgres to succeed;
   then re-pin the backends to the composed SHA and push their `develop`, then
   the frontend
4. E2E on Railway `development` (and/or the local docker compose stack:
   checkout `develop` per service, `docker compose up --build` — the `migrate`
   service applies the head)
5. One `develop` → `main` PR per service; merging the models-utils PR triggers
   `migrate.yml` against the production DB — **wait for it** before merging the
   backend PRs, whose merges trigger their Railway deploys. After the merge
   `main` == `develop` and `develop` carries forward unchanged
6. Tag the product release (`uplink-vX.Y.Z`, annotated) on the `main` merge
   commit; models-utils keeps its own semver in `setup.cfg`
7. Delete feature branches

After merge to `main`, the previously pinned feature-branch SHA remains valid
(it is part of main's history).

## Safety rules

- **Additive** changes (new columns/tables): safe once consuming service code
  is ready.
- **Destructive** changes (drop/rename): all consuming service code must be in
  production FIRST.
- Not every migration is reversible — `c1e_install_actions` uses
  `ALTER TYPE ... ADD VALUE` and has no downgrade, `ng2_topology_drop`,
  `ld1_legacy_drop` and `sh1_service_history_repair` are one-way (see
  [migrations.md](migrations.md) and [limitations.md](limitations.md)). Take a
  `pg_dump` of prod before merging a release that contains one.
- Parallel schema features get **separate** models-utils branches with each
  backend pinned to its own SHA — never combine unrelated schema changes.

## Current state (2026-10-06)

| Branch | SHA | `setup.cfg` | Alembic head | DB |
|---|---|---|---|---|
| `main` (prod, Uplink **v1.0.0**, tag `uplink-v1.0.0`) | `67c2af4` | 4.5.1 | `vw1_viewer_no_credential_read` | Railway `production`, migrated 2026-10-06 03:36 UTC |
| `develop` | `cad3ab4` | 5.1.0 | `pc1_provisioning_claim_token` | Railway `development`, migrated 2026-10-06 20:49 UTC |

On `develop`, not yet on `main` (the v1.0.1 cycle): 4.5.2 `computed`-block
hardening, 5.0.0 `pt2_unmap_port_labels` (doc 40 C8a — breaking for code that
reads `InventoryItem.parent_port`/`uplink_port`) and 5.1.0 provisioning
concurrency (`pc1_provisioning_claim_token`). Promotion order: models-utils
`develop` → `main`, wait for the prod migrate, then backend-erp + auth-erp,
then frontend-erp. See [../CHANGELOG.md](../CHANGELOG.md).
