"""Category icons: ROUTER -> 'router', OLT -> 'server'

Revision ID: ci1_category_icons
Revises: rt1_auth_refresh_token
Create Date: 2026-10-01

Data-only (no schema or model change). One lucide mapping across seed, DB,
backoffice and mobile: ROUTER drew `radio-tower` and OLT drew `radio` (wireless
glyphs for wired gear). `isp_seed.DEVICE_CATEGORIES` carries the new names for
fresh databases; this revision moves existing rows. A row whose icon was
customised through the API (anything other than the old default or NULL) is
left alone. inv1's `_ICON_BACKFILL` is history and keeps the old names.

Hand-written (NOT autogenerate), dc1 house style: lock_timeout, idempotent
(re-running matches nothing), reversible downgrade (only rows still on the
new default go back).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'ci1_category_icons'
down_revision: Union[str, Sequence[str], None] = 'rt1_auth_refresh_token'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# key -> (old default, new default)
_REMAP = {'ROUTER': ('radio-tower', 'router'), 'OLT': ('radio', 'server')}


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for key, (old, new) in _REMAP.items():
        connection.execute(
            text(
                "UPDATE device_category SET icon = :new "
                "WHERE key = :key AND (icon = :old OR icon IS NULL)"
            ),
            {"key": key, "old": old, "new": new},
        )
    print("[ci1_category_icons] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for key, (old, new) in _REMAP.items():
        connection.execute(
            text("UPDATE device_category SET icon = :old WHERE key = :key AND icon = :new"),
            {"key": key, "old": old, "new": new},
        )
    print("[ci1_category_icons] downgrade complete")
