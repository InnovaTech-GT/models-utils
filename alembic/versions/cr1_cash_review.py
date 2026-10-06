"""Cash-box admin review: SUBMITTED/REJECTED/APPROVED + review trail + permission

Revision ID: cr1_cash_review
Revises: pd1_client_payment_day
Create Date: 2026-10-03

The collector no longer closes the box: they SUBMIT it (counted cash + deposit
slip) and an admin APPROVEs or REJECTs. Additive:

- `cashsessionstatus += SUBMITTED, REJECTED, APPROVED` (CLOSED/DEPOSITED stay;
  existing rows untouched). Labels are added in an autocommit block and are NOT
  referenced by any statement here (PG forbids using a label in the
  transaction that created it).
- `cash_session`: `submitted_at`, `reviewed_at` (TIMESTAMPTZ), `reviewed_by`
  (FK user SET NULL), `review_note` (TEXT); index `ix_cash_session_company_status`.
- permission `cash_sessions.review`, granted to the global ADMIN role
  (rbac_seed.PERMISSIONS_DATA carries the row; the seed pass re-converges).

Enum labels cannot be dropped: downgrade drops columns/index/permission only.
Hand-written, house style: lock_timeout, IF NOT EXISTS, post-upgrade asserts.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "cr1_cash_review"
down_revision: Union[str, Sequence[str], None] = "pd1_client_payment_day"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against CashSessionStatus by tests/test_cash_review_models.py.
NEW_LABELS = ("SUBMITTED", "REJECTED", "APPROVED")
PERMISSION = {
    "name": "cash_sessions.review",
    "resource": "cash_sessions",
    "action": "review",
    "description": "Revisar, aprobar o rechazar cajas de cobradores",
}
_COLUMNS = (
    ("submitted_at", "TIMESTAMPTZ"),
    ("reviewed_at", "TIMESTAMPTZ"),
    ("review_note", "TEXT"),
    ("reviewed_by", "UUID"),
)
FK = "fk_cash_session_reviewed_by"
IDX = "ix_cash_session_company_status"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    with op.get_context().autocommit_block():
        for label in NEW_LABELS:
            op.execute(f"ALTER TYPE cashsessionstatus ADD VALUE IF NOT EXISTS '{label}'")

    for column, ddl in _COLUMNS:
        op.execute(f"ALTER TABLE cash_session ADD COLUMN IF NOT EXISTS {column} {ddl}")
    if connection.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": FK}).scalar() is None:
        op.execute(
            f'ALTER TABLE cash_session ADD CONSTRAINT {FK} FOREIGN KEY (reviewed_by) '
            'REFERENCES "user" (id) ON DELETE SET NULL'
        )
    op.execute(f"CREATE INDEX IF NOT EXISTS {IDX} ON cash_session (company_id, status)")

    connection.execute(
        text(
            "INSERT INTO permission (id, created_at, name, resource, action, description) "
            "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        PERMISSION,
    )
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r, permission p "
            "WHERE r.name = 'ADMIN' AND r.company_id IS NULL AND p.name = :perm "
            "ON CONFLICT DO NOTHING"
        ),
        {"perm": PERMISSION["name"]},
    )

    present = set(connection.execute(text(
        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'cashsessionstatus'"
    )).scalars())
    if not set(NEW_LABELS) <= present:
        raise RuntimeError(f"[cr1] cashsessionstatus labels missing: {set(NEW_LABELS) - present}")
    for column, _ in _COLUMNS:
        if connection.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'cash_session' AND column_name = :c"
        ), {"c": column}).scalar() is None:
            raise RuntimeError(f"[cr1] cash_session.{column} missing after upgrade")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.execute(
        text("DELETE FROM role_permission WHERE permission_id IN (SELECT id FROM permission WHERE name = :perm)"),
        {"perm": PERMISSION["name"]},
    )
    # The permission row itself stays: env.py's seed pass re-inserts it while
    # the seed lists it (cfg3 caveat). Enum labels cannot be dropped.
    op.execute(f"DROP INDEX IF EXISTS {IDX}")
    op.execute(f"ALTER TABLE cash_session DROP CONSTRAINT IF EXISTS {FK}")
    for column, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE cash_session DROP COLUMN IF EXISTS {column}")
