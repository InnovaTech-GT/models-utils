"""Referral and partner programs on SaaS billing (doc 69 §5)

Revision ID: rf1_referral_programs
Revises: et1_edge_telnet
Create Date: 2026-11-12

Purely additive; downgrade drops everything it adds. No data step, no PG enum:
statuses are VARCHAR + CHECK (pl1 precedent). Columns: company.referral_code
(unique), subscription.partner_since / balance_due_cents, billing_invoice
credit_cents / previous_balance_cents / carried_out_cents / credit_detail /
refunded_cents / recurrente_payment_id. Tables: referral, referral_reward,
billing_reversal, billing_card, company_identity, referral_identity, with the
ON DELETE rules of §5 (history outlives a deleted company). The down_revision
is provisional: it is re-pointed to the head of the moment at compose.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "rf1_referral_programs"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept literal (not imported from the models) so the revision never drifts with them.
_REWARD_KINDS = "'FREE_MONTH', 'REFERRER_CREDIT'"
_REWARD_STATUSES = ("'PENDING_APPROVAL', 'APPROVED', 'AVAILABLE', 'USED', 'REJECTED', "
                    "'INELIGIBLE', 'EXPIRED', 'FORFEITED', 'REVERSED'")

# A constraint inside ADD COLUMN IF NOT EXISTS is skipped with the column, so a re-run is a no-op.
_COLUMNS = (
    ("company", "referral_code",
     "VARCHAR(12) CONSTRAINT uq_company_referral_code UNIQUE"),
    ("subscription", "partner_since", "TIMESTAMPTZ"),
    ("subscription", "balance_due_cents",
     "INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_subscription_balance_due_nonneg CHECK (balance_due_cents >= 0)"),
    *(("billing_invoice", col,
       f"INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_billing_invoice_{col}_nonneg CHECK ({col} >= 0)")
      for col in ("credit_cents", "previous_balance_cents", "carried_out_cents", "refunded_cents")),
    ("billing_invoice", "credit_detail", "JSON"),
    ("billing_invoice", "recurrente_payment_id", "VARCHAR"),
)

_TABLES = (
    ("referral", """
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        referrer_company_id UUID REFERENCES company(id) ON DELETE SET NULL,
        referred_company_id UUID REFERENCES company(id) ON DELETE SET NULL,
        code VARCHAR(12) NOT NULL,
        referred_nit VARCHAR NOT NULL,
        referred_email VARCHAR NOT NULL,
        referred_phone VARCHAR NOT NULL,
        nit_seen_before BOOLEAN NOT NULL,
        free_period_start TIMESTAMPTZ,
        referrer_paid_at TIMESTAMPTZ,
        CONSTRAINT ck_referral_not_self CHECK
            (referrer_company_id IS NULL OR referrer_company_id <> referred_company_id),
        CONSTRAINT uq_referral_referred_company_id UNIQUE (referred_company_id)
    """),
    ("billing_reversal", """
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        kind VARCHAR(8) NOT NULL,
        gateway_id VARCHAR NOT NULL,
        status VARCHAR,
        reason VARCHAR,
        amount_cents INTEGER,
        invoice_id UUID REFERENCES billing_invoice(id) ON DELETE SET NULL,
        linked_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        payload JSON,
        applied_at TIMESTAMPTZ,
        CONSTRAINT ck_billing_reversal_kind CHECK (kind IN ('REFUND', 'DISPUTE')),
        CONSTRAINT uq_billing_reversal_gateway_id UNIQUE (gateway_id)
    """),
    ("referral_reward", f"""
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        referral_id UUID NOT NULL REFERENCES referral(id) ON DELETE CASCADE,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
        kind VARCHAR(16) NOT NULL,
        status VARCHAR(20) NOT NULL,
        amount_cents INTEGER,
        remaining_cents INTEGER NOT NULL DEFAULT 0,
        source_invoice_id UUID REFERENCES billing_invoice(id) ON DELETE SET NULL,
        unlock_invoice_id UUID REFERENCES billing_invoice(id) ON DELETE SET NULL,
        ineligible_reasons JSON,
        decided_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        decided_at TIMESTAMPTZ,
        decision_note TEXT,
        unlocked_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        reversal_id UUID REFERENCES billing_reversal(id) ON DELETE SET NULL,
        reversed_at TIMESTAMPTZ,
        forfeited_at TIMESTAMPTZ,
        clawed_cents INTEGER NOT NULL DEFAULT 0,
        CONSTRAINT ck_referral_reward_kind CHECK (kind IN ({_REWARD_KINDS})),
        CONSTRAINT ck_referral_reward_status CHECK (status IN ({_REWARD_STATUSES})),
        CONSTRAINT ck_referral_reward_amount_nonneg CHECK (amount_cents IS NULL OR amount_cents >= 0),
        CONSTRAINT ck_referral_reward_remaining CHECK
            (remaining_cents >= 0 AND remaining_cents <= COALESCE(amount_cents, 0)),
        CONSTRAINT ck_referral_reward_clawed_nonneg CHECK (clawed_cents >= 0),
        CONSTRAINT uq_referral_reward_referral_kind UNIQUE (referral_id, kind)
    """),
    ("billing_card", """
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
        payment_method_id VARCHAR NOT NULL,
        card_key VARCHAR(64) NOT NULL
    """),
    ("company_identity", """
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        company_id UUID REFERENCES company(id) ON DELETE SET NULL,
        kind VARCHAR(8) NOT NULL,
        value VARCHAR(255) NOT NULL,
        CONSTRAINT ck_company_identity_kind CHECK (kind IN ('NIT', 'EMAIL', 'PHONE')),
        CONSTRAINT uq_company_identity_company_kind_value UNIQUE (company_id, kind, value)
    """),
    ("referral_identity", """
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        referral_id UUID NOT NULL REFERENCES referral(id) ON DELETE CASCADE,
        kind VARCHAR(8) NOT NULL,
        value VARCHAR(255) NOT NULL,
        CONSTRAINT ck_referral_identity_kind CHECK (kind IN ('NIT', 'EMAIL', 'PHONE', 'CARD')),
        CONSTRAINT uq_referral_identity_referral_kind_value UNIQUE (referral_id, kind, value)
    """),
)

_INDEXES = (
    "ix_billing_invoice_recurrente_payment_id ON billing_invoice (recurrente_payment_id)",
    "ix_referral_referrer_company_id ON referral (referrer_company_id)",
    "ix_referral_reward_company_status_expires ON referral_reward (company_id, status, expires_at)",
    "ix_billing_card_company_id ON billing_card (company_id)",
    "ix_billing_card_card_key ON billing_card (card_key)",
    "ix_company_identity_company_id ON company_identity (company_id)",
    "ix_company_identity_kind_value ON company_identity (kind, value)",
)


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, column, ddl in _COLUMNS:
        c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))
    for table, body in _TABLES:
        c.execute(text(f"CREATE TABLE IF NOT EXISTS {table} ({body})"))
    for index in _INDEXES:
        c.execute(text(f"CREATE INDEX IF NOT EXISTS {index}"))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, _ in reversed(_TABLES):  # reverse FK order; drops their indexes too
        c.execute(text(f"DROP TABLE IF EXISTS {table}"))
    c.execute(text("DROP INDEX IF EXISTS ix_billing_invoice_recurrente_payment_id"))
    for table, column, _ in reversed(_COLUMNS):  # drops the column's CHECK / UNIQUE with it
        c.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
