"""ca1_collection_areas on a real Postgres: the table, its CHECKs and indexes,
the user home columns and the ADMIN grants exist at head; downgrade and upgrade
are idempotent. Run against a database already at `alembic upgrade head`; each
test runs in one rolled-back transaction."""
import json
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

SQUARE = json.dumps([[14.60, -90.55], [14.60, -90.50], [14.65, -90.50], [14.65, -90.55]])


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
    ca1 = load("versions/ca1_collection_areas.py", "ca1_collection_areas")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(ca1, fn)()


@pytest.fixture()
def company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=co, name=f"ca1-test-{co}")
    return co


def _insert(conn, co, name="A", color="#2563EB", polygon=SQUARE, collector=None):
    _x(conn, "INSERT INTO collection_area (id, created_at, updated_at, name, color, polygon, company_id, collector_id) "
             "VALUES (:id, now(), now(), :name, :color, CAST(:polygon AS json), :co, :collector)",
       id=uuid.uuid4(), name=name, color=color, polygon=polygon, co=co, collector=collector)


def _rejected(conn, fn):
    savepoint = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        fn()
    savepoint.rollback()


def test_checks_at_head(conn, company):
    _insert(conn, company)
    _rejected(conn, lambda: _insert(conn, company, name="bad color", color="blue"))
    _rejected(conn, lambda: _insert(conn, company, name="two points", polygon=json.dumps([[1, 2], [3, 4]])))
    _rejected(conn, lambda: _insert(conn, company, name="object", polygon=json.dumps({"a": 1})))
    _rejected(conn, lambda: _insert(conn, company, name="A"))  # same name, same company


def test_home_both_or_neither(conn, company):
    uid = uuid.uuid4()
    _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, email_verified, company_id, is_super_admin) '
             "VALUES (:id, now(), 'ca1', :email, 0, 'x', true, true, :co, false)",
       id=uid, email=f"ca1-{uid}@test.local", co=company)
    _x(conn, 'UPDATE "user" SET home_latitude = 14.6, home_longitude = -90.5 WHERE id = :id', id=uid)
    _rejected(conn, lambda: _x(conn, 'UPDATE "user" SET home_longitude = NULL WHERE id = :id', id=uid))
    _insert(conn, company, collector=uid)
    _rejected(conn, lambda: _insert(conn, company, name="second area", collector=uid))
    _x(conn, 'DELETE FROM "user" WHERE id = :id', id=uid)
    assert _x(conn, "SELECT collector_id FROM collection_area WHERE company_id = :co", co=company).scalar() is None


def test_admin_holds_permissions(conn):
    held = set(_x(conn, "SELECT p.name FROM role r JOIN role_permission rp ON rp.role_id = r.id "
                        "JOIN permission p ON p.id = rp.permission_id "
                        "WHERE r.name = 'ADMIN' AND r.company_id IS NULL AND p.resource = 'collection_areas'").scalars())
    assert held == {"collection_areas.read", "collection_areas.update"}


def test_round_trip(conn):
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # idempotent
    assert _x(conn, "SELECT to_regclass('collection_area')").scalar() is None
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _x(conn, "SELECT to_regclass('collection_area')").scalar() == "collection_area"
