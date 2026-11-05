"""Workflow engine v2 (doc 63-workflow-engine, plan 63a §2)

Revision ID: wf1_workflow_engine_v2
Revises: up1_usage_pricing
Create Date: 2026-11-03

Additive. executionstatus gains WAITING and CANCELLED (ALTER TYPE ... ADD
VALUE: irreversible, kept on downgrade). New tables workflow_version,
automation_template, workflow_event (outbox), service_boost. workflow gains
engine_version (legacy rows = 1) + active_version_id + template columns;
workflow_execution / workflow_step_execution gain the durable-run columns
(company_id backfilled from workflow); company.workflow_daily_message_cap;
provisioning_run.workflow_execution_id. The workflows.* permissions are seeded
by rbac_seed. Downgrade drops every new table and column. Hand-written, house
style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "wf1_workflow_engine_v2"
down_revision: str | Sequence[str] | None = "up1_usage_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_BOOST = "('STARTING', 'ACTIVE', 'ENDING', 'END_FAILED')"


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    # The new labels must be committed before the runnable index names WAITING.
    with op.get_context().autocommit_block():
        for value in ("WAITING", "CANCELLED"):
            op.execute(f"ALTER TYPE executionstatus ADD VALUE IF NOT EXISTS '{value}'")

    c.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_version (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            workflow_id UUID NOT NULL REFERENCES workflow(id) ON DELETE CASCADE,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            number INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'DRAFT',
            definition JSONB NOT NULL,
            trigger_type VARCHAR(16) NOT NULL,
            trigger_key VARCHAR(64) NOT NULL,
            source_template_version INTEGER,
            created_by UUID REFERENCES "user"(id) ON DELETE SET NULL,
            published_by UUID REFERENCES "user"(id) ON DELETE SET NULL,
            CONSTRAINT uq_workflow_version_number UNIQUE (workflow_id, number),
            CONSTRAINT ck_workflow_version_status CHECK (status IN ('DRAFT', 'PUBLISHED', 'SUPERSEDED')),
            CONSTRAINT ck_workflow_version_trigger_type CHECK (trigger_type IN ('event', 'date', 'bot')),
            CONSTRAINT ck_workflow_version_number CHECK (number >= 1)
        )
    """))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS automation_template (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            key VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL,
            name JSON NOT NULL,
            description JSON,
            category VARCHAR(32) NOT NULL,
            changelog JSON NOT NULL DEFAULT '[]'::json,
            params JSON NOT NULL DEFAULT '[]'::json,
            requires JSON NOT NULL DEFAULT '{}'::json,
            definition JSONB NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            CONSTRAINT uq_automation_template_key_version UNIQUE (key, version),
            CONSTRAINT ck_automation_template_version CHECK (version >= 1)
        )
    """))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_event (
            id BIGSERIAL PRIMARY KEY,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            name VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            depth SMALLINT NOT NULL DEFAULT 0,
            source_run_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
    """))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS service_boost (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            client_service_id UUID NOT NULL REFERENCES client_service(id) ON DELETE CASCADE,
            extra_mb INTEGER NOT NULL,
            base_download_mbps INTEGER,
            base_upload_mbps INTEGER,
            boost_download_mbps INTEGER,
            boost_upload_mbps INTEGER,
            starts_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'STARTING',
            end_reason VARCHAR(24),
            start_run_id UUID,
            end_run_id UUID,
            workflow_execution_id UUID,
            end_attempts INTEGER NOT NULL DEFAULT 0,
            ended_at TIMESTAMPTZ,
            CONSTRAINT ck_service_boost_extra_mb CHECK (extra_mb > 0),
            CONSTRAINT ck_service_boost_end_attempts CHECK (end_attempts >= 0),
            CONSTRAINT ck_service_boost_status CHECK (status IN
                ('STARTING', 'ACTIVE', 'ENDING', 'ENDED', 'FAILED', 'END_FAILED')),
            CONSTRAINT ck_service_boost_end_reason CHECK (end_reason IS NULL OR end_reason IN
                ('EXPIRED', 'SUSPENDED', 'CANCELLED', 'PLAN_CHANGED', 'START_FAILED'))
        )
    """))

    for ddl in (
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS engine_version SMALLINT NOT NULL DEFAULT 1",
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS active_version_id UUID",
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS source_template_key VARCHAR(64)",
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS source_template_version INTEGER",
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS template_params JSON",
        "ALTER TABLE workflow ADD COLUMN IF NOT EXISTS ignored_template_version INTEGER",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        ("ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS company_id UUID "
         "REFERENCES company(id) ON DELETE CASCADE"),
        ("ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS version_id UUID "
         "REFERENCES workflow_version(id) ON DELETE SET NULL"),
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS definition JSON",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(160)",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS depth SMALLINT NOT NULL DEFAULT 0",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS test_options JSON",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS context JSON",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS cursor JSON",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS resume_at TIMESTAMPTZ",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS claim_token UUID",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS client_id UUID",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS client_service_id UUID",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS customer_message TEXT",
        "ALTER TABLE workflow_execution ADD COLUMN IF NOT EXISTS error_code VARCHAR(48)",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS node_key VARCHAR(32)",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS action_key VARCHAR(64)",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS simulated BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS input JSON",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS error_code VARCHAR(48)",
        "ALTER TABLE workflow_step_execution ADD COLUMN IF NOT EXISTS resume_at TIMESTAMPTZ",
        "ALTER TABLE company ADD COLUMN IF NOT EXISTS workflow_daily_message_cap INTEGER",
        "ALTER TABLE provisioning_run ADD COLUMN IF NOT EXISTS workflow_execution_id UUID",
    ):
        c.execute(text(ddl))

    # workflow <-> workflow_version is a cycle: the FK goes on after both exist.
    c.execute(text("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_workflow_active_version') THEN
                ALTER TABLE workflow ADD CONSTRAINT fk_workflow_active_version
                    FOREIGN KEY (active_version_id) REFERENCES workflow_version(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_workflow_engine_version') THEN
                ALTER TABLE workflow ADD CONSTRAINT ck_workflow_engine_version
                    CHECK (engine_version IN (1, 2));
            END IF;
        END $$
    """))

    c.execute(text(
        "UPDATE workflow_execution e SET company_id = w.company_id "
        "FROM workflow w WHERE e.workflow_id = w.id AND e.company_id IS NULL"
    ))

    for ddl in (
        "CREATE INDEX IF NOT EXISTS ix_workflow_version_workflow_id ON workflow_version (workflow_id)",
        "CREATE INDEX IF NOT EXISTS ix_workflow_version_company_id ON workflow_version (company_id)",
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_version_draft "
         "ON workflow_version (workflow_id) WHERE status = 'DRAFT'"),
        "CREATE INDEX IF NOT EXISTS ix_workflow_version_trigger ON workflow_version (company_id, trigger_key)",
        ("CREATE INDEX IF NOT EXISTS ix_workflow_event_pending "
         "ON workflow_event (id) WHERE processed_at IS NULL"),
        "CREATE INDEX IF NOT EXISTS ix_service_boost_company_id ON service_boost (company_id)",
        "CREATE INDEX IF NOT EXISTS ix_service_boost_client_service_id ON service_boost (client_service_id)",
        (f"CREATE UNIQUE INDEX IF NOT EXISTS uq_service_boost_open "
         f"ON service_boost (client_service_id) WHERE status IN {_OPEN_BOOST}"),
        ("CREATE INDEX IF NOT EXISTS ix_service_boost_due "
         "ON service_boost (expires_at) WHERE status = 'ACTIVE'"),
        "CREATE INDEX IF NOT EXISTS ix_workflow_execution_company_id ON workflow_execution (company_id)",
        "CREATE INDEX IF NOT EXISTS ix_workflow_execution_client_id ON workflow_execution (client_id)",
        ("CREATE INDEX IF NOT EXISTS ix_workflow_execution_client_service_id "
         "ON workflow_execution (client_service_id)"),
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_execution_dedupe "
         "ON workflow_execution (workflow_id, dedupe_key) WHERE dedupe_key IS NOT NULL"),
        ("CREATE INDEX IF NOT EXISTS ix_workflow_execution_runnable ON workflow_execution (resume_at) "
         "WHERE status IN ('PENDING', 'WAITING') AND definition IS NOT NULL"),
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_step_execution_node "
         "ON workflow_step_execution (execution_id, node_key) WHERE node_key IS NOT NULL"),
        ("CREATE INDEX IF NOT EXISTS ix_step_execution_messages "
         "ON workflow_step_execution (action_key, completed_at)"),
    ):
        c.execute(text(ddl))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    # Dropping a column drops its indexes and FKs; the enum labels stay.
    for table, cols in (
        ("workflow_step_execution",
         ("node_key", "action_key", "attempts", "simulated", "input", "error_code", "resume_at")),
        ("workflow_execution",
         ("updated_at", "company_id", "version_id", "definition", "dedupe_key", "depth", "is_test",
          "test_options", "context", "cursor", "resume_at", "claim_token", "heartbeat_at", "client_id",
          "client_service_id", "customer_message", "error_code")),
        ("workflow",
         ("engine_version", "active_version_id", "source_template_key", "source_template_version",
          "template_params", "ignored_template_version")),
        ("company", ("workflow_daily_message_cap",)),
        ("provisioning_run", ("workflow_execution_id",)),
    ):
        for col in cols:
            c.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"))
    for table in ("service_boost", "workflow_event", "automation_template", "workflow_version"):
        c.execute(text(f"DROP TABLE IF EXISTS {table}"))
