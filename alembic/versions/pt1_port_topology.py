"""port-level topology: device-type port templates, item ports, network links

Revision ID: pt1_port_topology
Revises: sh1_service_history_repair
Create Date: 2026-10-04

Doc 40 §3.1.1 (uplink-workspace/docs/isp-platform/40-port-level-topology-design.md).
Strictly ADDITIVE and inert: no rows are created, nothing existing is
rewritten. The schema only starts carrying data once a backend that writes
links (cycle C2) is deployed.

What this adds:

1. device_type.port_template (JSON list of port groups) + path_role, and
   ck_device_type_ports_serialized (lot types have no physical ports).
2. uq_inventory_item_id_company — the target of the composite FKs below.
   Always satisfiable: id is the PK.
3. inventory_item_port — one row per physical port. Names unique per item
   case-insensitively; PON ports also unique on (slot, number, direction).
4. network_link — a device's one upstream link, port to port. The composite
   (port, item, company) FKs make cross-tenant links and links naming another
   item's port impossible. fk_link_down_port is NO ACTION, not CASCADE:
   deleting only a device's own port must not silently drop its link (Postgres
   runs NO ACTION checks after the statement's cascades, so deleting the whole
   leaf ONU still passes).
5. Two DEFERRABLE INITIALLY DEFERRED constraint triggers asserting, at COMMIT,
   that every link agrees with parent_id:
   inventory_item[down_item_id].parent_id = up_item_id. A writer that bypasses
   the backend helper fails closed with NETWORK_LINK_PARENT_MISMATCH. Inert
   until links exist, so they are safe alongside old backends.

The triggers live only here, never in SQLAlchemy metadata: every consuming
service's test suite builds its schema with SQLite create_all, which cannot
parse plpgsql (ng1 precedent).

downgrade() refuses while any network_link row or any origin = 'ITEM' port
exists (iv1_insights_v2 precedent): links recorded in the field cannot be
reconstructed. Template-generated ports are regenerable and are dropped.

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertions, total downgrade.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "pt1_port_topology"
down_revision: str | Sequence[str] | None = "sh1_service_history_repair"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors database_utils.models.isp (PORT_MEDIA, PORT_DIRECTIONS, PORT_ORIGINS,
# NETWORK_LINK_SOURCES); tests/test_port_topology.py pins them equal.
PORT_MEDIA = ("ETH", "PON")
PORT_DIRECTIONS = ("UP", "DOWN", "ANY")
PORT_ORIGINS = ("TEMPLATE", "ITEM")
NETWORK_LINK_SOURCES = ("OFFICE", "FIELD", "IMPORT")

_NEW_TABLES = ("inventory_item_port", "network_link")
_NEW_TRIGGERS = ("trg_network_link_parent_sync", "trg_inventory_item_link_sync")

_ASSERT_PARENT_FN = """
CREATE OR REPLACE FUNCTION network_link_assert_parent(p_item uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM network_link l JOIN inventory_item i ON i.id = l.down_item_id
             WHERE l.down_item_id = p_item AND i.parent_id IS DISTINCT FROM l.up_item_id) THEN
    RAISE EXCEPTION 'NETWORK_LINK_PARENT_MISMATCH: item % does not hang from its upstream link', p_item;
  END IF;
END $$;
"""

_LINK_SYNC_FN = """
CREATE OR REPLACE FUNCTION trg_fn_network_link_parent_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN PERFORM network_link_assert_parent(NEW.down_item_id); RETURN NULL; END $$;
"""

_ITEM_SYNC_FN = """
CREATE OR REPLACE FUNCTION trg_fn_inventory_item_link_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN PERFORM network_link_assert_parent(NEW.id); RETURN NULL; END $$;
"""


def _in(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def _add_constraint(connection, table: str, name: str, body: str) -> None:
    """ADD CONSTRAINT unless it already exists. Not DROP-then-ADD: once the
    composite FKs depend on uq_inventory_item_id_company it cannot be dropped."""
    exists = connection.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}
    ).first()
    if not exists:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {body}")


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. device_type ---------------------------------------------------
    op.execute("ALTER TABLE device_type ADD COLUMN IF NOT EXISTS port_template JSON NULL")
    op.execute("ALTER TABLE device_type ADD COLUMN IF NOT EXISTS path_role VARCHAR(32) NULL")
    _add_constraint(connection, "device_type", "ck_device_type_ports_serialized",
                    "CHECK (port_template IS NULL OR is_serialized)")

    # --- 2. composite FK target -------------------------------------------
    _add_constraint(connection, "inventory_item", "uq_inventory_item_id_company",
                    "UNIQUE (id, company_id)")

    # --- 3. inventory_item_port -------------------------------------------
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS inventory_item_port (
          id          UUID PRIMARY KEY,
          company_id  UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
          item_id     UUID NOT NULL,
          name        VARCHAR(32) NOT NULL,
          slot        SMALLINT NULL,
          number      SMALLINT NOT NULL,
          medium      VARCHAR(8) NOT NULL,
          direction   VARCHAR(4) NOT NULL,
          origin      VARCHAR(8) NOT NULL,
          created_at  TIMESTAMPTZ NOT NULL,
          updated_at  TIMESTAMPTZ NOT NULL,
          CONSTRAINT fk_item_port_item FOREIGN KEY (item_id, company_id)
            REFERENCES inventory_item (id, company_id) ON DELETE CASCADE,
          CONSTRAINT uq_item_port_identity UNIQUE (id, item_id, company_id),
          CONSTRAINT ck_item_port_slot CHECK (slot BETWEEN 0 AND 255),
          CONSTRAINT ck_item_port_number CHECK (number BETWEEN 0 AND 4095),
          CONSTRAINT ck_item_port_medium CHECK (medium IN ({_in(PORT_MEDIA)})),
          CONSTRAINT ck_item_port_direction CHECK (direction IN ({_in(PORT_DIRECTIONS)})),
          CONSTRAINT ck_item_port_origin CHECK (origin IN ({_in(PORT_ORIGINS)}))
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_item_port_name "
        "ON inventory_item_port (item_id, lower(name))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_item_port_pon_number ON inventory_item_port "
        "(item_id, coalesce(slot, -1), number, direction) WHERE medium = 'PON'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_port_company ON inventory_item_port (company_id)"
    )

    # --- 4. network_link --------------------------------------------------
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS network_link (
          id            UUID PRIMARY KEY,
          company_id    UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
          up_item_id    UUID NOT NULL,
          up_port_id    UUID NOT NULL,
          down_item_id  UUID NOT NULL,
          down_port_id  UUID NULL,
          source        VARCHAR(8) NOT NULL,
          task_id       UUID NULL REFERENCES task(id) ON DELETE SET NULL,
          created_by_id UUID NULL REFERENCES "user"(id) ON DELETE SET NULL,
          created_at    TIMESTAMPTZ NOT NULL,
          updated_at    TIMESTAMPTZ NOT NULL,
          CONSTRAINT fk_link_up_port FOREIGN KEY (up_port_id, up_item_id, company_id)
            REFERENCES inventory_item_port (id, item_id, company_id),
          CONSTRAINT fk_link_down_item FOREIGN KEY (down_item_id, company_id)
            REFERENCES inventory_item (id, company_id) ON DELETE CASCADE,
          CONSTRAINT fk_link_down_port FOREIGN KEY (down_port_id, down_item_id, company_id)
            REFERENCES inventory_item_port (id, item_id, company_id),
          CONSTRAINT uq_link_up_port   UNIQUE (up_port_id),
          CONSTRAINT uq_link_down_port UNIQUE (down_port_id),
          CONSTRAINT uq_link_down_item UNIQUE (down_item_id),
          CONSTRAINT ck_link_not_self  CHECK (up_item_id <> down_item_id),
          CONSTRAINT ck_link_source    CHECK (source IN ({_in(NETWORK_LINK_SOURCES)}))
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_network_link_up_item ON network_link (up_item_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_network_link_company ON network_link (company_id)"
    )

    # --- 5. link <-> parent_id backstop (deferred, checked at COMMIT) ------
    op.execute(_ASSERT_PARENT_FN)
    op.execute(_LINK_SYNC_FN)
    op.execute(_ITEM_SYNC_FN)
    op.execute("DROP TRIGGER IF EXISTS trg_network_link_parent_sync ON network_link")
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_network_link_parent_sync
          AFTER INSERT OR UPDATE ON network_link
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION trg_fn_network_link_parent_sync()
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_inventory_item_link_sync ON inventory_item")
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_inventory_item_link_sync
          AFTER UPDATE OF parent_id ON inventory_item
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION trg_fn_inventory_item_link_sync()
    """)

    # --- post-upgrade assertions ------------------------------------------
    tables = connection.execute(text(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(:names)"
    ), {"names": list(_NEW_TABLES)}).scalar()
    triggers = connection.execute(text(
        "SELECT count(*) FROM pg_trigger WHERE tgname = ANY(:names) AND tgdeferrable"
    ), {"names": list(_NEW_TRIGGERS)}).scalar()
    if tables != len(_NEW_TABLES) or triggers != len(_NEW_TRIGGERS):
        raise RuntimeError(
            f"[pt1] post-upgrade check failed: {tables} tables, {triggers} deferred triggers"
        )
    print("[pt1_port_topology] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    links = connection.execute(text("SELECT count(*) FROM network_link")).scalar()
    item_ports = connection.execute(text(
        "SELECT count(*) FROM inventory_item_port WHERE origin = 'ITEM'"
    )).scalar()
    if links or item_ports:
        raise RuntimeError(
            f"[pt1] refusing downgrade: {links} network_link row(s) and {item_ports} "
            "per-item port(s) exist and cannot be reconstructed; delete them first"
        )

    op.execute("DROP TRIGGER IF EXISTS trg_inventory_item_link_sync ON inventory_item")
    op.execute("DROP TRIGGER IF EXISTS trg_network_link_parent_sync ON network_link")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_inventory_item_link_sync()")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_network_link_parent_sync()")
    op.execute("DROP FUNCTION IF EXISTS network_link_assert_parent(uuid)")
    op.execute("DROP TABLE IF EXISTS network_link")
    op.execute("DROP TABLE IF EXISTS inventory_item_port")
    op.execute(
        "ALTER TABLE inventory_item DROP CONSTRAINT IF EXISTS uq_inventory_item_id_company"
    )
    op.execute(
        "ALTER TABLE device_type DROP CONSTRAINT IF EXISTS ck_device_type_ports_serialized"
    )
    op.execute("ALTER TABLE device_type DROP COLUMN IF EXISTS path_role")
    op.execute("ALTER TABLE device_type DROP COLUMN IF EXISTS port_template")
    print("[pt1_port_topology] downgrade complete")
