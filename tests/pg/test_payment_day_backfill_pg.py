"""pd2_payment_day_backfill on a real Postgres.

Run against a database already at `alembic upgrade head`. Each test builds its
own company and clients inside one rolled-back transaction and calls the
revision's upgrade() again; existing rows already carry a payment_day, so only
the test's NULL clients are written.
"""
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

GT = ZoneInfo("America/Guatemala")


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


@pytest.fixture()
def company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=co, name=f"pd2-test-{co}")
    return co


def _client(conn, co, payment_day=None):
    cid = uuid.uuid4()
    _x(conn, "INSERT INTO client (id, created_at, name, company_id, payment_day) "
             "VALUES (:id, now(), 'pd2', :co, :d)", id=cid, co=co, d=payment_day)
    return cid


def _pay(conn, co, client_id, *paid_at, kind="PAYMENT", reverses=None):
    oid = uuid.uuid4()
    _x(conn, 'INSERT INTO "order" (id, created_at, total, paid, company_id, client_id) '
             "VALUES (:id, now(), 100, true, :co, :c)", id=oid, co=co, c=client_id)
    ids = []
    for at in paid_at:
        pid = uuid.uuid4()
        _x(conn, "INSERT INTO payment (id, created_at, company_id, order_id, kind, amount_cents, "
                 "paid_at, reverses_payment_id) VALUES (:id, now(), :co, :o, :k, 10000, :at, :r)",
           id=pid, co=co, o=oid, k=kind, at=at, r=reverses)
        ids.append(pid)
    return ids


def _gt(y, m, d, h=10):
    return datetime(y, m, d, h, tzinfo=GT)


def _upgrade(conn):
    pd2 = load("versions/pd2_payment_day_backfill.py", "pd2_payment_day_backfill")
    with Operations.context(MigrationContext.configure(conn)):
        pd2.upgrade()


def _day(conn, client_id):
    return _x(conn, "SELECT payment_day FROM client WHERE id = :id", id=client_id).scalar()


def test_most_frequent_day_wins(conn, company):
    c = _client(conn, company)
    _pay(conn, company, c, _gt(2026, 6, 5), _gt(2026, 7, 5), _gt(2026, 8, 20), _gt(2026, 9, 5))
    _upgrade(conn)
    assert _day(conn, c) == 5


def test_tie_goes_to_the_most_recent_date(conn, company):
    c = _client(conn, company)
    _pay(conn, company, c, _gt(2026, 6, 5), _gt(2026, 7, 20), _gt(2026, 8, 5), _gt(2026, 9, 20))
    _upgrade(conn)
    assert _day(conn, c) == 20


def test_several_payments_on_one_date_count_once(conn, company):
    c = _client(conn, company)
    _pay(conn, company, c, _gt(2026, 9, 28), _gt(2026, 9, 28, 11), _gt(2026, 9, 28, 12))
    _pay(conn, company, c, _gt(2026, 7, 10), _gt(2026, 8, 10))
    _upgrade(conn)
    assert _day(conn, c) == 10


def test_day_is_taken_in_guatemala_time(conn, company):
    c = _client(conn, company)
    # 2026-09-06 03:00 UTC is still 2026-09-05 21:00 in Guatemala.
    utc = datetime(2026, 9, 6, 3, tzinfo=ZoneInfo("UTC"))
    _pay(conn, company, c, utc)
    _upgrade(conn)
    assert _day(conn, c) == 5


def test_reversed_payments_and_refunds_are_ignored(conn, company):
    c = _client(conn, company)
    (reversed_id,) = _pay(conn, company, c, _gt(2026, 9, 25))
    _pay(conn, company, c, _gt(2026, 9, 26), kind="REFUND", reverses=reversed_id)
    _pay(conn, company, c, _gt(2026, 8, 12))
    _upgrade(conn)
    assert _day(conn, c) == 12


def test_no_history_gets_the_default(conn, company):
    c = _client(conn, company)
    _upgrade(conn)
    assert _day(conn, c) == 15


def test_existing_value_is_kept_and_rerun_is_a_noop(conn, company):
    kept = _client(conn, company, payment_day=3)
    _pay(conn, company, kept, _gt(2026, 9, 20), _gt(2026, 8, 20))
    _upgrade(conn)
    _upgrade(conn)
    assert _day(conn, kept) == 3
