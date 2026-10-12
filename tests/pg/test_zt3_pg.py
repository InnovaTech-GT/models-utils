"""zt3_ztp_always_on on a real Postgres: the column is gone at head; the
downgrade re-adds it NOT NULL default false (existing rows read false) and the
upgrade drops it again, both idempotent. Run against a database already at
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
    zt3 = load("versions/zt3_ztp_always_on.py", "zt3_ztp_always_on")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(zt3, fn)()


def _column(conn):
    return _x(conn, "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'provisioning_settings' AND column_name = 'ztp_enabled'"
              ).first()


def test_downgrade_restores_switch_off_then_upgrade_drops_it(conn):
    assert _column(conn) is None
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"zt3-{co}")
    _x(conn, "INSERT INTO provisioning_settings (id, created_at, updated_at, company_id) "
             "VALUES (:id, now(), now(), :co)", id=uuid.uuid4(), co=co)
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # idempotent
    nullable, default = _column(conn)
    assert nullable == "NO" and default == "false"
    assert _x(conn, "SELECT ztp_enabled FROM provisioning_settings WHERE company_id = :co",
              co=co).scalar() is False
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _column(conn) is None
