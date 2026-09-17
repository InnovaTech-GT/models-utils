"""Configuración/Perfil: user.phone, user.photo_url, user_invitation.phone

Revision ID: cfg1_user_profile
Revises: 6e7506e57be9
Create Date: 2026-09-15

Figma redesign PR 2 (docs/design/plans/02-configuracion.md §2.1), first of
three. The Perfil sub-page edits a phone number and shows an avatar; the
Empleados invite form (PR 6) carries the phone through to the created user.
Neither field exists today.

PR 6's separate `em1_user_phone` revision is FOLDED IN HERE (master plan
§2.4) — there is exactly one revision for these three columns.

All three columns are nullable VARCHAR with no backfill and no index (never
filtered on). Phone format is not validated at the DB level: Guatemalan
`5698-5824` and international numbers must both fit; validation lives in the
zod/Pydantic layer.

`photo_url` is a plain URL rather than an FK into `uploaded_file`:
# ponytail: plain URL, no file table — auth-erp has no file storage and the
# uploadedfileownertype PG enum has no USER label. Promote to an
# uploaded_file FK the day avatar uploads actually ship.

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertion, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'cfg1_user_profile'
down_revision: Union[str, Sequence[str], None] = '6e7506e57be9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# "user" is a reserved word — always double-quoted (t2/ba1 precedent).
_NEW_COLUMNS = (
    ("user", "phone"),
    ("user", "photo_url"),
    ("user_invitation", "phone"),
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone VARCHAR')
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS photo_url VARCHAR')
    op.execute('ALTER TABLE user_invitation ADD COLUMN IF NOT EXISTS phone VARCHAR')

    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[cfg1] expected column(s) missing after upgrade: {missing}")

    print("[cfg1_user_profile] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute('ALTER TABLE user_invitation DROP COLUMN IF EXISTS phone')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS photo_url')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS phone')

    print("[cfg1_user_profile] downgrade complete")
