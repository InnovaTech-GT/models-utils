"""wb1_whatsapp_bot_config on a real Postgres: at head the table exists with
its server defaults, one row per company (uq_whatsapp_bot_config_company)
and ON DELETE CASCADE from company; the downgrade drops it and the upgrade
re-creates it, both idempotent. Run against a database already at
`alembic upgrade head`; each test runs in one rolled-back transaction."""
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
    wb1 = load("versions/wb1_whatsapp_bot_config.py", "wb1_whatsapp_bot_config")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(wb1, fn)()


def _table(conn):
    return _x(conn, "SELECT to_regclass('whatsapp_bot_config')").scalar()


def _index(conn):
    return _x(conn, "SELECT indexdef FROM pg_indexes WHERE tablename = 'whatsapp_bot_config' "
                    "AND indexname = 'uq_whatsapp_bot_config_company'").scalar()


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"wb1-{co}")
    return co


def _insert(conn, co):
    return _x(conn, "INSERT INTO whatsapp_bot_config (id, created_at, updated_at, company_id, "
                    "welcome_text) VALUES (:id, now(), now(), :co, 'Hola') RETURNING id",
              id=uuid.uuid4(), co=co).scalar()


def test_head_has_the_table_with_defaults_unique_company_and_cascade(conn):
    assert "UNIQUE INDEX" in _index(conn)
    co = _company(conn)
    row_id = _insert(conn, co)
    row = _x(conn, "SELECT enabled, menu_button_text, options::text "
                   "FROM whatsapp_bot_config WHERE id = :id", id=row_id).one()
    assert tuple(row) == (True, "Ver opciones", "[]")
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _insert(conn, co)  # one row per company
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _insert(conn, uuid.uuid4())  # company FK
    _x(conn, "DELETE FROM company WHERE id = :co", co=co)
    assert _x(conn, "SELECT count(*) FROM whatsapp_bot_config WHERE company_id = :co",
              co=co).scalar() == 0


def test_downgrade_drops_then_upgrade_recreates(conn):
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # idempotent
    assert _table(conn) is None
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _table(conn) == "whatsapp_bot_config"
    assert _index(conn) is not None
