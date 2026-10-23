"""Collection areas: one polygon per collector + the collector's home + permissions

Revision ID: ca1_collection_areas
Revises: pz1_provisioning_zone
Create Date: 2026-10-16

The back-office gets Configuración -> Áreas de cobro: each area is a polygon
drawn on the map with at most one collector, and the collector ETL gives a
client to the collector of the area that contains it. Additive:

- new table `collection_area` (company CASCADE, collector FK user SET NULL,
  name unique per company, one area per collector, `polygon` JSON ring of
  [lat, lng] pairs) + CHECKs on the color format and the polygon shape.
- `user` + `home_latitude`, `home_longitude` (FLOAT, NULL) and
  `ck_user_home_location` (both or neither): where a collector lives, used to
  suggest each collector's area.
- permissions `collection_areas.read` / `collection_areas.update`, granted to
  the global ADMIN role (rbac_seed.PERMISSIONS_DATA carries the rows; the seed
  pass re-converges and gives read to VIEWER through its filter).

The CHECKs live here only: SQLite create_all in the consumers cannot parse the
PG regex operator or jsonb functions. Downgrade drops the table, the columns
and the grants. Hand-written, house style: lock_timeout, IF [NOT] EXISTS,
post-upgrade asserts.
"""
from typing import Sequence, Union

from sqlalchemy.sql import text

from alembic import op

revision: str = "ca1_collection_areas"
down_revision: Union[str, Sequence[str], None] = "pz1_provisioning_zone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against rbac_seed.PERMISSIONS_DATA by tests/test_collection_area.py.
PERMISSIONS = (
    {
        "name": "collection_areas.read",
        "resource": "collection_areas",
        "action": "read",
        "description": "Ver las áreas de cobro",
    },
    {
        "name": "collection_areas.update",
        "resource": "collection_areas",
        "action": "update",
        "description": "Crear, editar y eliminar áreas de cobro y la ubicación de la casa de los cobradores",
    },
)
HOME_CHECK = "ck_user_home_location"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("""
        CREATE TABLE IF NOT EXISTS collection_area (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            name VARCHAR(80) NOT NULL,
            color VARCHAR(7) NOT NULL,
            polygon JSON NOT NULL,
            company_id UUID NOT NULL REFERENCES company (id) ON DELETE CASCADE,
            collector_id UUID NULL REFERENCES "user" (id) ON DELETE SET NULL,
            CONSTRAINT uq_collection_area_company_name UNIQUE (company_id, name),
            CONSTRAINT ck_collection_area_color CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
            CONSTRAINT ck_collection_area_polygon CHECK (
                json_typeof(polygon) = 'array' AND json_array_length(polygon) >= 3
            )
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_collection_area_company_id ON collection_area (company_id)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_collection_area_collector "
        "ON collection_area (collector_id) WHERE collector_id IS NOT NULL"
    )

    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS home_latitude DOUBLE PRECISION')
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS home_longitude DOUBLE PRECISION')
    if connection.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": HOME_CHECK}).scalar() is None:
        op.execute(
            f'ALTER TABLE "user" ADD CONSTRAINT {HOME_CHECK} '
            "CHECK ((home_latitude IS NULL) = (home_longitude IS NULL))"
        )

    for permission in PERMISSIONS:
        connection.execute(
            text(
                "INSERT INTO permission (id, created_at, name, resource, action, description) "
                "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            permission,
        )
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = 'ADMIN' AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"perm": permission["name"]},
        )

    indexes = set(connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'collection_area'"
    )).scalars())
    missing = {"ix_collection_area_company_id", "uq_collection_area_collector"} - indexes
    if missing:
        raise RuntimeError(f"[ca1] collection_area indexes missing after upgrade: {missing}")
    for column in ("home_latitude", "home_longitude"):
        if connection.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'user' AND column_name = :c"
        ), {"c": column}).scalar() is None:
            raise RuntimeError(f"[ca1] user.{column} missing after upgrade")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for permission in PERMISSIONS:
        connection.execute(
            text("DELETE FROM role_permission WHERE permission_id IN (SELECT id FROM permission WHERE name = :perm)"),
            {"perm": permission["name"]},
        )
    # The permission rows stay: env.py's seed pass re-inserts them while the
    # seed lists them (same as cr1).
    op.execute(f'ALTER TABLE "user" DROP CONSTRAINT IF EXISTS {HOME_CHECK}')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS home_longitude')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS home_latitude')
    op.execute("DROP TABLE IF EXISTS collection_area")
