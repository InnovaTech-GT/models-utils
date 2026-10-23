"""ZTP trigger: ztp_enabled switch, ZTP_* notification kinds, push outbox, push tokens

Revision ID: zt1_ztp_trigger
Revises: pe1_playbook_phases
Create Date: 2026-10-08

ZTP SP2 (doc 43 §4). Additive:

provisioning_settings  + ztp_enabled BOOLEAN NOT NULL DEFAULT false (metadata-only)
user_notification      ~ ck_user_notification_kind + the four ZTP_* kinds
                       + push_state VARCHAR(12) NULL (the push outbox)
                       + ix_user_notification_push_pending (created_at) WHERE PENDING
user_push_token        new (Expo push tokens, token UNIQUE, platform/app CHECKs)

The CHECK literals and the index predicate are this revision's own copies
(revisions are immutable); tests/test_zt1_ztp_trigger.py pins them to
database_utils/models/auth.py. Downgrade deletes the ZTP_* rows first (the old
CHECK rejects them), then undoes the rest.

Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "zt1_ztp_trigger"
down_revision: Union[str, Sequence[str], None] = "pe1_playbook_phases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KIND_CHECK = (
    "kind IN ('TASK_ASSIGNED','TASK_OVERDUE','PAYMENTS_OVERDUE',"
    "'ZTP_SUCCEEDED','ZTP_FAILED','ZTP_NEEDS_ATTENTION','ZTP_ROLLBACK_INCOMPLETE')"
)
_KIND_CHECK_PRE = "kind IN ('TASK_ASSIGNED','TASK_OVERDUE','PAYMENTS_OVERDUE')"
_PUSH_PENDING_WHERE = "push_state = 'PENDING'"
_PLATFORM_CHECK = "platform IN ('android','ios')"
_APP_CHECK = "app IN ('tecnicos')"


def _kind_check(c, literal: str) -> None:
    c.execute(text(
        "ALTER TABLE user_notification DROP CONSTRAINT IF EXISTS ck_user_notification_kind"
    ))
    c.execute(text(
        f"ALTER TABLE user_notification ADD CONSTRAINT ck_user_notification_kind CHECK ({literal})"
    ))


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text(
        "ALTER TABLE provisioning_settings "
        "ADD COLUMN IF NOT EXISTS ztp_enabled BOOLEAN NOT NULL DEFAULT false"
    ))
    _kind_check(c, _KIND_CHECK)
    c.execute(text(
        "ALTER TABLE user_notification ADD COLUMN IF NOT EXISTS push_state VARCHAR(12) NULL"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_user_notification_push_pending "
        f"ON user_notification (created_at) WHERE {_PUSH_PENDING_WHERE}"
    ))
    c.execute(text(f"""
        CREATE TABLE IF NOT EXISTS user_push_token (
            id UUID NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            token VARCHAR(255) NOT NULL,
            platform VARCHAR(8) NOT NULL,
            app VARCHAR(16) NOT NULL,
            company_id UUID NOT NULL,
            user_id UUID NOT NULL,
            CONSTRAINT pk_user_push_token PRIMARY KEY (id),
            CONSTRAINT uq_user_push_token_token UNIQUE (token),
            CONSTRAINT fk_user_push_token_company_id FOREIGN KEY (company_id)
                REFERENCES company (id) ON DELETE CASCADE,
            CONSTRAINT fk_user_push_token_user_id FOREIGN KEY (user_id)
                REFERENCES "user" (id) ON DELETE CASCADE,
            CONSTRAINT ck_user_push_token_platform CHECK ({_PLATFORM_CHECK}),
            CONSTRAINT ck_user_push_token_app CHECK ({_APP_CHECK})
        )
    """))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_user_push_token_user_id ON user_push_token (user_id)"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS user_push_token"))
    c.execute(text("DROP INDEX IF EXISTS ix_user_notification_push_pending"))
    c.execute(text("ALTER TABLE user_notification DROP COLUMN IF EXISTS push_state"))
    c.execute(text("DELETE FROM user_notification WHERE kind LIKE 'ZTP\\_%'"))
    _kind_check(c, _KIND_CHECK_PRE)
    c.execute(text("ALTER TABLE provisioning_settings DROP COLUMN IF EXISTS ztp_enabled"))
