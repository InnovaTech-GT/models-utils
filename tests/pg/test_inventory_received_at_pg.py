"""ri1_inventory_received_at on a real Postgres (doc 47).

Run against a database already at `alembic upgrade head` (see
test_port_topology_pg.py). Each test runs in one rolled-back transaction: it
downgrades ri1 (drops the column), inserts pre-ri1 rows, and re-runs the
upgrade, so the backfill-before-NOT-NULL order is exercised on real data.
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


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _run(conn, fn):
    ri1 = load("versions/ri1_inventory_received_at.py", "ri1_inventory_received_at")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(ri1, fn)()


@pytest.fixture()
def plant(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=co, name=f"ri1-test-{co}")
    t = uuid.uuid4()
    _x(conn, "INSERT INTO device_type (id, created_at, updated_at, name, company_id, "
             "category_id, is_serialized) SELECT :id, now(), now(), 'ri1-type', :co, id, true "
             "FROM device_category LIMIT 1", id=t, co=co)
    return {"co": co, "t": t}


def _item(conn, p, created_at="now()"):
    iid = uuid.uuid4()
    _x(conn, "INSERT INTO inventory_item (id, created_at, updated_at, company_id, "
             f"device_type_id, network_attached) VALUES (:id, {created_at}, now(), :co, :t, false)",
             id=iid, co=p["co"], t=p["t"])
    return iid


def _received(conn, iid):
    return _x(conn, "SELECT received_at, created_at FROM inventory_item WHERE id = :i", i=iid).one()


def test_upgrade_backfills_existing_rows_from_created_at(conn, plant):
    _run(conn, "downgrade")
    old = _item(conn, plant, created_at="now() - interval '400 days'")
    _run(conn, "upgrade")
    received_at, created_at = _received(conn, old)
    assert received_at == created_at


def test_head_column_is_not_null_and_defaults_to_now(conn, plant):
    nullable, default = _x(conn, "SELECT is_nullable, column_default FROM information_schema.columns "
                                 "WHERE table_name = 'inventory_item' AND column_name = 'received_at'").one()
    assert nullable == "NO"
    assert "now()" in default
    received_at, _ = _received(conn, _item(conn, plant))  # insert omits received_at
    assert received_at is not None


def test_downgrade_upgrade_round_trip_is_idempotent(conn, plant):
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # DROP COLUMN IF EXISTS
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # ADD COLUMN IF NOT EXISTS + no-op backfill
    assert _received(conn, _item(conn, plant))[0] is not None
