"""ch1_client_chat on a real Postgres: integration flag defaults, the client_chat
ON CONFLICT upsert on the real unique index, FK cascades, and an idempotent
downgrade/upgrade. Each test runs in one rolled-back transaction."""
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

UPSERT = (
    "INSERT INTO client_chat (id, created_at, updated_at, company_id, client_id, integration_id, address, username) "
    "VALUES (:id, now(), now(), :co, :cl, :i, :a, :u) "
    "ON CONFLICT (client_id, integration_id, address) DO UPDATE SET username = EXCLUDED.username"
)


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


def _setup(conn):
    co, cl, i = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"ch1-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)", id=cl, co=co)
    _x(conn, "INSERT INTO integration (id, created_at, updated_at, name, base_url, auth_type, company_id) "
             "VALUES (:id, now(), now(), 'i', 'https://x', 'NONE', :co)", id=i, co=co)
    return co, cl, i


def test_integration_flag_defaults(conn):
    _, _, i = _setup(conn)
    assert tuple(_x(conn, "SELECT is_default, menu_enabled FROM integration WHERE id = :i", i=i).one()) == (False, True)


def test_on_conflict_upsert_and_household(conn):
    co, cl, i = _setup(conn)
    _x(conn, UPSERT, id=uuid.uuid4(), co=co, cl=cl, i=i, a="123", u=None)
    _x(conn, UPSERT, id=uuid.uuid4(), co=co, cl=cl, i=i, a="123", u="juan")
    assert _x(conn, "SELECT username FROM client_chat WHERE client_id = :c", c=cl).all() == [("juan",)]
    # Another client on the same chat is allowed.
    cl2 = uuid.uuid4()
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c2', :co)", id=cl2, co=co)
    _x(conn, UPSERT, id=uuid.uuid4(), co=co, cl=cl2, i=i, a="123", u="juan")
    assert _x(conn, "SELECT count(*) FROM client_chat WHERE integration_id = :i AND address = '123'", i=i).scalar() == 2


def test_cascades(conn):
    co, cl, i = _setup(conn)
    _x(conn, UPSERT, id=uuid.uuid4(), co=co, cl=cl, i=i, a="1", u=None)
    _x(conn, "DELETE FROM integration WHERE id = :i", i=i)
    assert _x(conn, "SELECT count(*) FROM client_chat WHERE client_id = :c", c=cl).scalar() == 0
    co, cl, i = _setup(conn)
    _x(conn, UPSERT, id=uuid.uuid4(), co=co, cl=cl, i=i, a="1", u=None)
    _x(conn, "DELETE FROM client WHERE id = :c", c=cl)
    assert _x(conn, "SELECT count(*) FROM client_chat WHERE integration_id = :i", i=i).scalar() == 0


def test_downgrade_then_upgrade_is_idempotent(conn):
    ch1 = load("versions/ch1_client_chat.py", "ch1_client_chat")
    cols = ("SELECT count(*) FROM information_schema.columns WHERE table_name = 'integration' "
            "AND column_name IN ('is_default', 'menu_enabled')")
    with Operations.context(MigrationContext.configure(conn)):
        ch1.downgrade()
        ch1.downgrade()
        assert _x(conn, cols).scalar() == 0
        assert _x(conn, "SELECT to_regclass('client_chat')").scalar() is None
        ch1.upgrade()
        ch1.upgrade()
    assert _x(conn, cols).scalar() == 2
    assert _x(conn, "SELECT to_regclass('uq_client_chat')").scalar() == "uq_client_chat"
