# models-utils — Documentation Wiki

Wiki for the `database-utils` shared library, the schema authority of the Uplink
ISP platform. Start with the [repo README](../README.md) for a quickstart.

## Core

| Page | One-liner |
|---|---|
| [overview.md](overview.md) | What the library is, its role in Uplink, full feature inventory |
| [architecture.md](architecture.md) | Internal layering, the two execution surfaces, import-direction constraint |
| [connections.md](connections.md) | How backend-erp / auth-erp / cron-erp / frontend-erp relate to this library; env vars |
| [external-dependencies.md](external-dependencies.md) | Python dependencies and external systems (PostgreSQL, GitHub Actions, Railway) |
| [deployment-local.md](deployment-local.md) | Local docker compose `migrate` service + standalone dev commands |
| [deployment-production.md](deployment-production.md) | CI pipeline, prod migration via GitHub Actions, branch/release model |
| [limitations.md](limitations.md) | Known limitations, TODOs, and tracked debt |

## Features

| Page | One-liner |
|---|---|
| [auth-models.md](auth-models.md) | Auth, tenancy, RBAC, email/password tokens, and SaaS-billing models |
| [crm-models.md](crm-models.md) | CRM models: clients, orders, payments ledger, tasks, custom fields, integrations |
| [isp-models.md](isp-models.md) | ISP vertical models: service plans, client services, inventory, playbooks, provisioning runs and jobs |
| [network-models.md](network-models.md) | Network config models (Cycles 5+7): GenieACS/TR-069 tables, ProvisioningJob extensions, PENDING_INFORM status, core-config columns (tiers, mgmt surface, install state) — **plus the Cycle-10 company network graph**: the `inventory_item` tree + its two guard triggers, `is_passive`, the playbook binding tables, `ProvisioningRun` |
| [workflow-models.md](workflow-models.md) | Workflow automation models: templates, triggers, step DAG, executions |
| [workflow-engine.md](workflow-engine.md) | Engine semantics: trigger matching, DAG execution, the two `ENQUEUE_PROVISIONING` modes, provisioning resolution |
| [schemas.md](schemas.md) | The 42 Pydantic v2 schema modules |
| [utilities.md](utilities.md) | The 24 shared utility modules (network graph, provisioning resolution + runs, JWT, permissions, audit, SSRF, OTEL, ...) |
| [email-service.md](email-service.md) | Transactional email service, SMTP implementation, Jinja2 templates |
| [migrations.md](migrations.md) | Alembic workflow, revision chains, seed scripts |
