"""ii1_integration_interest on a real Postgres: the ON CONFLICT on the COALESCE
expression index (keeps created_at, bumps updated_at), cascades, and an
idempotent downgrade/upgrade. Each test runs in one rolled-back transaction."""
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
    "INSERT INTO integration_interest (id, created_at, updated_at, company_id, provider, type, note) "
    "VALUES (:id, :t, :t, :co, :p, 'PAYMENTS', :n) "
    "ON CONFLICT (company_id, type, (COALESCE(provider, ''))) DO UPDATE "
    "SET updated_at = EXCLUDED.updated_at, note = EXCLUDED.note"
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


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"ii1-{co}")
    return co


def test_on_conflict_expression_upsert(conn):
    co = _company(conn)
    for p in ("STRIPE", None):
        _x(conn, UPSERT, id=uuid.uuid4(), t="2026-01-01T00:00:00Z", co=co, p=p, n="first")
        _x(conn, UPSERT, id=uuid.uuid4(), t="2026-02-01T00:00:00Z", co=co, p=p, n="second")
    rows = _x(conn, "SELECT provider, note, created_at < updated_at FROM integration_interest "
                    "WHERE company_id = :c ORDER BY provider NULLS LAST", c=co).all()
    assert [tuple(r) for r in rows] == [("STRIPE", "second", True), (None, "second", True)]


def test_company_delete_cascades(conn):
    co = _company(conn)
    _x(conn, UPSERT, id=uuid.uuid4(), t="2026-01-01T00:00:00Z", co=co, p="X", n="n")
    _x(conn, "DELETE FROM company WHERE id = :c", c=co)
    assert _x(conn, "SELECT count(*) FROM integration_interest WHERE company_id = :c", c=co).scalar() == 0


def test_downgrade_then_upgrade_is_idempotent(conn):
    ii1 = load("versions/ii1_integration_interest.py", "ii1_integration_interest")
    with Operations.context(MigrationContext.configure(conn)):
        ii1.downgrade()
        ii1.downgrade()
        assert _x(conn, "SELECT to_regclass('integration_interest')").scalar() is None
        ii1.upgrade()
        ii1.upgrade()
    assert _x(conn, "SELECT to_regclass('uq_integration_interest')").scalar() == "uq_integration_interest"
