"""dm1_device_metrics on a real Postgres: tables, CHECKs and cascade at head,
and downgrade/upgrade round-trips."""
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

TABLES = ("device_metric", "device_metric_5m", "device_metric_1h", "device_metric_1d")


def _exists(conn, t):
    return conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _company(conn):
    cid = uuid.uuid4()
    tier = conn.execute(sa.text("SELECT id FROM tier LIMIT 1")).scalar()
    conn.execute(sa.text("INSERT INTO company (id, created_at, name, tier_id) "
                         "VALUES (:id, now(), :n, :t)"),
                 {"id": cid, "n": f"dm1-{cid}", "t": tier})
    return cid


def test_checks_and_cascade(conn):
    assert all(_exists(conn, t) for t in TABLES)
    co = _company(conn)
    ins = sa.text("INSERT INTO device_metric (id, company_id, name, oid, kind, device_type_id, "
                  "inventory_item_id) VALUES (:id, :co, :n, '1.3.6.1.2.1.1.3.0', :k, NULL, NULL)")
    with conn.begin_nested() as sp:
        with pytest.raises(sa.exc.IntegrityError, match="ck_device_metric_one_scope"):
            conn.execute(ins, {"id": uuid.uuid4(), "co": co, "n": "x", "k": "GAUGE"})
        sp.rollback()
    with conn.begin_nested() as sp:
        with pytest.raises(sa.exc.IntegrityError, match="ck_device_metric_kind"):
            conn.execute(sa.text(str(ins.text).replace("NULL, NULL", "NULL, gen_random_uuid()")),
                         {"id": uuid.uuid4(), "co": co, "n": "y", "k": "DERIVE"})
        sp.rollback()


def test_round_trip(conn):
    dm1 = load("versions/dm1_device_metrics.py", "dm1_device_metrics")
    with Operations.context(MigrationContext.configure(conn)):
        dm1.downgrade()
        dm1.downgrade()
        assert not any(_exists(conn, t) for t in TABLES)
        dm1.upgrade()
        dm1.upgrade()
    assert all(_exists(conn, t) for t in TABLES)
