"""auth_refresh_token: server-side refresh-token store (reuse detection)

Revision ID: rt1_auth_refresh_token
Revises: mi2_mobile_field_ops
Create Date: 2026-09-30

bug-fix/refresh-token-reuse. ADDITIVE ONLY: one new table, nothing existing
is touched. Refresh JWTs now carry a `jti`; auth-erp records each one here so
/refresh can reject a rotated token and revoke its family on reuse, and a
logout can revoke a family. Tokens issued before this revision have no row
(and maybe no jti) — auth-erp accepts them once and migrates them into a new
family, so no backfill is needed.

Hand-written, house style: lock_timeout, IF NOT EXISTS, named constraints,
post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "rt1_auth_refresh_token"
down_revision: Union[str, Sequence[str], None] = "mi2_mobile_field_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("""
        CREATE TABLE IF NOT EXISTS auth_refresh_token (
            jti VARCHAR(64) NOT NULL,
            family_id UUID NOT NULL,
            user_id UUID NOT NULL,
            company_id UUID,
            client_type VARCHAR(10) NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            rotated_at TIMESTAMPTZ,
            replaced_by VARCHAR(64),
            revoked_at TIMESTAMPTZ,
            CONSTRAINT pk_auth_refresh_token PRIMARY KEY (jti),
            CONSTRAINT fk_auth_refresh_token_user_id FOREIGN KEY (user_id) REFERENCES "user" (id) ON DELETE CASCADE,
            CONSTRAINT fk_auth_refresh_token_company_id FOREIGN KEY (company_id) REFERENCES company (id) ON DELETE CASCADE,
            CONSTRAINT ck_auth_refresh_token_client_type CHECK (client_type IN ('web','mobile'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_auth_refresh_token_family_id ON auth_refresh_token (family_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_auth_refresh_token_user_id ON auth_refresh_token (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_auth_refresh_token_expires_at ON auth_refresh_token (expires_at)")

    assert connection.execute(text("SELECT to_regclass('auth_refresh_token')")).scalar() is not None


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("DROP TABLE IF EXISTS auth_refresh_token")
