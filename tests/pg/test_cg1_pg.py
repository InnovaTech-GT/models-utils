"""cg1_client_groups on a real Postgres: the client_group CHECKs, the FK actions
(group delete nulls its members, company delete cascades), dunning_run.mode OFF,
and an idempotent downgrade/upgrade that maps OFF runs to REPORT. Each test runs
in one rolled-back transaction."""
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


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"cg1-{co}")
    return co


def _group(conn, co, **cols):
    g = uuid.uuid4()
    row = {"color": "grey", "auto_suspension_mode": None, "min_overdue_orders": None, **cols}
    _x(conn, "INSERT INTO client_group (id, created_at, updated_at, company_id, name, color, "
             "auto_suspension_mode, min_overdue_orders) VALUES (:id, now(), now(), :co, :n, :c, :m, :mn)",
       id=g, co=co, n=f"g-{g}", c=row["color"], m=row["auto_suspension_mode"], mn=row["min_overdue_orders"])
    return g


def _client(conn, co, group=None):
    cl = uuid.uuid4()
    _x(conn, "INSERT INTO client (id, created_at, name, company_id, client_group_id) "
             "VALUES (:id, now(), 'c', :co, :g)", id=cl, co=co, g=group)
    return cl


def _run(conn, co, mode):
    r = uuid.uuid4()
    _x(conn, "INSERT INTO dunning_run (id, ran_at, mode, outcome, candidates, suspended, skipped, company_id) "
             "VALUES (:id, now(), :m, 'OK', '[]', '[]', '[]', :co)", id=r, m=mode, co=co)
    return r


@pytest.mark.parametrize("cols", [{"color": "#fff"}, {"auto_suspension_mode": "MAYBE"}, {"min_overdue_orders": 0}])
def test_checks_reject_bad_rows(conn, cols):
    co = _company(conn)
    nested = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        _group(conn, co, **cols)
    nested.rollback()
    # A good row still goes in after the failed savepoint.
    _group(conn, co, color="red", auto_suspension_mode="OFF", min_overdue_orders=3)


def test_group_delete_nulls_members_and_company_delete_cascades(conn):
    co = _company(conn)
    g = _group(conn, co)
    cl = _client(conn, co, g)
    _x(conn, "DELETE FROM client_group WHERE id = :g", g=g)
    assert _x(conn, "SELECT client_group_id FROM client WHERE id = :c", c=cl).scalar() is None
    g2 = _group(conn, co)
    _x(conn, "DELETE FROM client WHERE company_id = :co", co=co)
    _x(conn, "DELETE FROM company WHERE id = :co", co=co)
    assert _x(conn, "SELECT count(*) FROM client_group WHERE id = :g", g=g2).scalar() == 0


def test_dunning_run_accepts_off(conn):
    co = _company(conn)
    r = _run(conn, co, "OFF")
    assert _x(conn, "SELECT mode FROM dunning_run WHERE id = :r", r=r).scalar() == "OFF"


def test_downgrade_then_upgrade_is_idempotent_and_maps_off_runs(conn):
    cg1 = load("versions/cg1_client_groups.py", "cg1_client_groups")
    co = _company(conn)
    r = _run(conn, co, "OFF")
    with Operations.context(MigrationContext.configure(conn)):
        cg1.downgrade()
        cg1.downgrade()
        assert _x(conn, "SELECT mode FROM dunning_run WHERE id = :r", r=r).scalar() == "REPORT"
        assert _x(conn, "SELECT to_regclass('client_group')").scalar() is None
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            _run(conn, co, "OFF")
        nested.rollback()
        cg1.upgrade()
        cg1.upgrade()
    assert _x(conn, "SELECT to_regclass('client_group')").scalar() == "client_group"
    assert _x(conn, "SELECT to_regclass('ix_client_client_group')").scalar() == "ix_client_client_group"
    _run(conn, co, "OFF")
