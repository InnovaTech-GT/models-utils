"""pt2_unmap_port_labels / pt3_drop_port_labels on a real Postgres (doc 40 §4.2 C8).

Run against a database already at `alembic upgrade head` (see
test_port_topology_pg.py). Each test runs in one rolled-back transaction. The
label columns are re-added inside it when a later head (pt3) dropped them, so
the legacy-row scenario can always be built.
"""
import os
import uuid

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

INDEX = "uq_inventory_item_parent_port"


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        for col in ("parent_port", "uplink_port"):
            connection.execute(sa.text(
                f"ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS {col} VARCHAR(64)"
            ))
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _index_exists(conn):
    return _x(conn, f"SELECT to_regclass('{INDEX}')").scalar() is not None


def _run(conn, fn, rev="pt2_unmap_port_labels"):
    mod = load(f"versions/{rev}.py", rev)
    with Operations.context(MigrationContext.configure(conn)):
        getattr(mod, fn)()


def _label_columns(conn):
    return {r[0] for r in _x(conn, "SELECT column_name FROM information_schema.columns "
                                   "WHERE table_schema = current_schema() "
                                   "AND table_name = 'inventory_item' "
                                   "AND column_name IN ('parent_port', 'uplink_port')")}


def _item(conn, co, type_id, parent=None, label=None):
    iid = uuid.uuid4()
    _x(conn, "INSERT INTO inventory_item (id, created_at, updated_at, company_id, "
             "device_type_id, network_attached, parent_id, parent_port) "
             "VALUES (:id, now(), now(), :co, :t, true, :p, :l)",
             id=iid, co=co, t=type_id, p=parent, l=label)
    return iid


@pytest.fixture()
def plant(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=co, name=f"pt2-test-{co}")
    t = uuid.uuid4()
    _x(conn, "INSERT INTO device_type (id, created_at, updated_at, name, company_id, "
             "category_id, is_serialized) SELECT :id, now(), now(), 'pt2-type', :co, id, true "
             "FROM device_category LIMIT 1", id=t, co=co)
    a, b = _item(conn, co, t), _item(conn, co, t)
    return {"co": co, "t": t, "a": a, "b": b}


def test_head_has_no_label_index(conn):
    assert not _index_exists(conn)


def test_reparenting_next_to_a_sibling_with_the_same_legacy_label_succeeds(conn, plant):
    """A C8a backend no longer clears the moved item's label (review blocker)."""
    p = plant
    _item(conn, p["co"], p["t"], parent=p["b"], label="PON 1")
    moved = _item(conn, p["co"], p["t"], parent=p["a"], label="PON 1")
    _x(conn, "UPDATE inventory_item SET parent_id = :b WHERE id = :i", b=p["b"], i=moved)
    assert _x(conn, "SELECT count(*) FROM inventory_item WHERE parent_id = :b "
                    "AND parent_port = 'PON 1'", b=p["b"]).scalar() == 2


def test_downgrade_restores_the_index_and_upgrade_drops_it_again(conn):
    _run(conn, "downgrade")
    assert _index_exists(conn)
    _run(conn, "upgrade")
    assert not _index_exists(conn)
    _run(conn, "upgrade")  # idempotent


def test_pt3_drops_the_labelled_columns_reruns_and_downgrades_without_the_index(conn, plant):
    _item(conn, plant["co"], plant["t"], parent=plant["a"], label="PON 1")
    _run(conn, "upgrade", "pt3_drop_port_labels")
    assert _label_columns(conn) == set() and not _index_exists(conn)
    _run(conn, "upgrade", "pt3_drop_port_labels")  # idempotent re-run
    _run(conn, "downgrade", "pt3_drop_port_labels")
    assert _label_columns(conn) == {"parent_port", "uplink_port"}
    assert not _index_exists(conn)  # pt2's downgrade owns the index
