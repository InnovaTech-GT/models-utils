"""payment_link: links de pago en línea (Recurrente) por orden

Revision ID: pl1_payment_link
Revises: cp1_dunning
Create Date: 2026-10-27

payment_link (una fila por link pedido a la pasarela). Aditiva: tabla nueva,
índices y CHECKs. Downgrade la borra. Hand-written, house style:
lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "pl1_payment_link"
down_revision: str | Sequence[str] | None = "cp1_dunning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_link (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            client_id UUID NOT NULL REFERENCES client(id) ON DELETE RESTRICT,
            order_id UUID NOT NULL REFERENCES "order"(id) ON DELETE RESTRICT,
            integration_id UUID REFERENCES integration(id) ON DELETE SET NULL,
            provider VARCHAR(32) NOT NULL DEFAULT 'RECURRENTE',
            status VARCHAR(16) NOT NULL DEFAULT 'CREATING',
            amount_cents BIGINT NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'GTQ',
            payment_methods JSON NOT NULL DEFAULT '[]'::json,
            expires_at TIMESTAMPTZ NOT NULL,
            checkout_id VARCHAR(64),
            checkout_url VARCHAR(512),
            live_mode BOOLEAN,
            last_checked_at TIMESTAMPTZ,
            checks INTEGER NOT NULL DEFAULT 0,
            intent_id VARCHAR(64),
            payment_id UUID REFERENCES payment(id) ON DELETE RESTRICT,
            paid_at TIMESTAMPTZ,
            review_reason VARCHAR(64),
            CONSTRAINT ck_payment_link_status CHECK (status IN
                ('CREATING', 'PENDING', 'PAID', 'EXPIRED', 'REVIEW', 'FAILED')),
            CONSTRAINT ck_payment_link_provider CHECK (provider IN ('RECURRENTE')),
            CONSTRAINT ck_payment_link_amount CHECK (amount_cents >= 500),
            CONSTRAINT ck_payment_link_currency CHECK (currency IN ('GTQ', 'USD')),
            CONSTRAINT ck_payment_link_checks CHECK (checks >= 0),
            -- Fuera de CREATING/FAILED siempre hay un checkout de la pasarela.
            CONSTRAINT ck_payment_link_has_checkout CHECK (
                status IN ('CREATING', 'FAILED')
                OR (checkout_id IS NOT NULL AND checkout_url IS NOT NULL)),
            -- PAID solo con intento, pago de Uplink y fecha.
            CONSTRAINT ck_payment_link_paid CHECK (
                status <> 'PAID'
                OR (intent_id IS NOT NULL AND payment_id IS NOT NULL AND paid_at IS NOT NULL)),
            CONSTRAINT ck_payment_link_review CHECK (
                status <> 'REVIEW' OR review_reason IS NOT NULL)
        )
    """))
    for ddl in (
        "CREATE INDEX IF NOT EXISTS ix_payment_link_company_id ON payment_link (company_id)",
        "CREATE INDEX IF NOT EXISTS ix_payment_link_client_id ON payment_link (client_id)",
        "CREATE INDEX IF NOT EXISTS ix_payment_link_company_status ON payment_link (company_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_payment_link_order ON payment_link (order_id)",
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_link_checkout "
        "ON payment_link (checkout_id) WHERE checkout_id IS NOT NULL"),
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_link_intent "
        "ON payment_link (intent_id) WHERE intent_id IS NOT NULL"),
        ("CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_link_active_order "
        "ON payment_link (order_id) WHERE status IN ('CREATING', 'PENDING')"),
    ):
        c.execute(text(ddl))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS payment_link"))
