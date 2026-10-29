"""pl2_payment_link_support: user.is_system + uploadedfileownertype PAYMENT_LINK

Revision ID: pl2_payment_link_support
Revises: pl1_payment_link
Create Date: 2026-10-28

Two small pieces the online-payments cycle needs (backend-erp):

1. `user.is_system` BOOLEAN NOT NULL DEFAULT false: a machine account, e.g. the
   per-company "Bot de WhatsApp" user that receives online payments. It does not
   take a seat in the tier (`utils/tier_limits.py`) and auth-erp refuses its
   login. `ck_user_system_not_super_admin`: a system account is never a super
   admin.
2. `uploadedfileownertype` += 'PAYMENT_LINK': a transfer receipt for a payment
   link that is not confirmed yet (owner_id = payment_link.id). 'PAYMENT' stays
   for a recorded payment's evidence (pm1).

IRREVERSIBLE in part: Postgres cannot DROP an enum label, so downgrade() drops
the column and the CHECK and keeps 'PAYMENT_LINK' (precedents pm1, po1). No
statement here may NAME the new label after adding it (same transaction rule).

Hand-written, house style: lock_timeout, IF [NOT] EXISTS, post-upgrade checks.
"""
from sqlalchemy.sql import text

from alembic import op

revision = "pl2_payment_link_support"
down_revision = "pl1_payment_link"
branch_labels = None
depends_on = None

_ENUM_NAME = "uploadedfileownertype"
_NEW_LABEL = "PAYMENT_LINK"


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))

    # Native-enum ADD VALUE always runs in an autocommit_block (pm1/po1).
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {_ENUM_NAME} ADD VALUE IF NOT EXISTS '{_NEW_LABEL}'")

    # "user" is a reserved word: always quoted.
    c.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT false'))
    c.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_system_not_super_admin'))
    c.execute(text(
        'ALTER TABLE "user" ADD CONSTRAINT ck_user_system_not_super_admin '
        "CHECK (NOT (is_system AND is_super_admin))"
    ))

    # --- checks ---------------------------------------------------------------------
    labels = set(c.execute(text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
    ), {"name": _ENUM_NAME}).scalars())
    if _NEW_LABEL not in labels:
        raise RuntimeError(f"[pl2] {_ENUM_NAME} lacks {_NEW_LABEL} after upgrade")
    if c.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'user' AND column_name = 'is_system'"
    )).scalar() is None:
        raise RuntimeError("[pl2] user.is_system missing after upgrade")
    print("[pl2_payment_link_support] upgrade complete")


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_system_not_super_admin'))
    c.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS is_system'))
    # PostgreSQL cannot DROP an enum label: 'PAYMENT_LINK' stays (pm1/po1 shape).
    print("[pl2_payment_link_support] enum label kept (PG cannot drop enum labels)")
