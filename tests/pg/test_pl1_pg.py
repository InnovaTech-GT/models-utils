"""pl1_payment_link on a real Postgres at head: the CHECKs refuse a link that
breaks the payment rules (PAID without intent/payment/date, REVIEW without a
reason, below Q5, unknown status/provider/currency, a checkout missing outside
CREATING/FAILED), one CREATING/PENDING link per order, the FKs keep the order
and client while a link points at them, a company delete still cascades, and
the downgrade/upgrade pair is idempotent. Each test runs in one rolled-back
transaction."""
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

CHECKS = {
    "ck_payment_link_status", "ck_payment_link_provider", "ck_payment_link_amount",
    "ck_payment_link_currency", "ck_payment_link_checks", "ck_payment_link_has_checkout",
    "ck_payment_link_paid", "ck_payment_link_review",
}
INDEXES = {"uq_payment_link_active_order", "uq_payment_link_checkout", "uq_payment_link_intent"}


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


def _fixture(conn):
    co, cl, order = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"pl1-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)",
       id=cl, co=co)
    _x(conn, 'INSERT INTO "order" (id, created_at, total, paid, company_id, client_id) '
             "VALUES (:id, now(), 150, false, :co, :cl)", id=order, co=co, cl=cl)
    return co, cl, order


def _link(conn, co, cl, order, **kw):
    row = {
        "id": uuid.uuid4(), "co": co, "cl": cl, "order": order, "status": "PENDING",
        "amount": 15000, "currency": "GTQ", "provider": "RECURRENTE", "checks": 0,
        "checkout_id": f"ch_{uuid.uuid4().hex[:16]}", "checkout_url": "https://app.recurrente.com/x",
        "intent_id": None, "paid_at": None, "review_reason": None,
    }
    row.update(kw)
    _x(conn, "INSERT INTO payment_link (id, created_at, updated_at, company_id, client_id, order_id, "
             "status, amount_cents, currency, provider, checks, expires_at, checkout_id, checkout_url, "
             "intent_id, paid_at, review_reason) VALUES (:id, now(), now(), :co, :cl, :order, :status, "
             ":amount, :currency, :provider, :checks, now() + interval '5 days', :checkout_id, "
             ":checkout_url, :intent_id, :paid_at, :review_reason)", **row)
    return row["id"]


def _refused(conn, fn):
    with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
        fn()


def test_head_has_the_table_its_checks_and_indexes(conn):
    assert _x(conn, "SELECT to_regclass('payment_link')").scalar() == "payment_link"
    checks = set(_x(conn, "SELECT conname FROM pg_constraint WHERE conrelid = 'payment_link'::regclass "
                          "AND contype = 'c'").scalars())
    assert checks == CHECKS
    idx = set(_x(conn, "SELECT indexname FROM pg_indexes WHERE tablename = 'payment_link'").scalars())
    assert INDEXES <= idx


def test_a_valid_link_and_its_defaults(conn):
    co, cl, order = _fixture(conn)
    _x(conn, "INSERT INTO payment_link (id, created_at, updated_at, company_id, client_id, order_id, "
             "amount_cents, expires_at) VALUES (:id, now(), now(), :co, :cl, :o, 500, now())",
       id=uuid.uuid4(), co=co, cl=cl, o=order)
    row = _x(conn, "SELECT status, provider, currency, checks, payment_methods::text "
                   "FROM payment_link WHERE order_id = :o", o=order).one()
    assert tuple(row) == ("CREATING", "RECURRENTE", "GTQ", 0, "[]")


def test_one_active_link_per_order(conn):
    co, cl, order = _fixture(conn)
    _link(conn, co, cl, order)
    _refused(conn, lambda: _link(conn, co, cl, order, status="CREATING", checkout_id=None, checkout_url=None))
    _link(conn, co, cl, order, status="EXPIRED")
    _link(conn, co, cl, order, status="REVIEW", review_reason="AMOUNT_MISMATCH")
    _link(conn, co, cl, order, status="FAILED", checkout_id=None, checkout_url=None)


def test_same_checkout_or_intent_twice_is_refused(conn):
    co, cl, order = _fixture(conn)
    _, _, order2 = _fixture(conn)
    _link(conn, co, cl, order, status="EXPIRED", checkout_id="ch_dup", intent_id="in_dup")
    _refused(conn, lambda: _link(conn, co, cl, order2, status="EXPIRED", checkout_id="ch_dup"))
    _refused(conn, lambda: _link(conn, co, cl, order2, status="EXPIRED", intent_id="in_dup"))


@pytest.mark.parametrize("bad", [
    {"amount": 499},
    {"status": "CANCELLED"},
    {"provider": "PAYPAL"},
    {"currency": "EUR"},
    {"checks": -1},
    {"checkout_id": None},                                   # PENDING sin checkout
    {"checkout_url": None},
    {"status": "PAID", "intent_id": "in_x", "paid_at": None},  # PAID sin pago ni fecha
    {"status": "PAID"},                                      # PAID sin nada
    {"status": "REVIEW"},                                    # REVIEW sin motivo
])
def test_checks_refuse_broken_links(conn, bad):
    co, cl, order = _fixture(conn)
    _refused(conn, lambda: _link(conn, co, cl, order, **bad))


def test_order_and_client_are_kept_while_a_link_points_at_them(conn):
    co, cl, order = _fixture(conn)
    _link(conn, co, cl, order, status="EXPIRED")
    _refused(conn, lambda: _x(conn, 'DELETE FROM "order" WHERE id = :o', o=order))
    _refused(conn, lambda: _x(conn, "DELETE FROM client WHERE id = :c", c=cl))


def test_company_delete_still_cascades(conn):
    co, cl, order = _fixture(conn)
    _link(conn, co, cl, order)
    _x(conn, "DELETE FROM company WHERE id = :co", co=co)
    assert _x(conn, "SELECT count(*) FROM payment_link WHERE company_id = :co", co=co).scalar() == 0


def test_downgrade_then_upgrade_is_idempotent(conn):
    pl1 = load("versions/pl1_payment_link.py", "pl1_payment_link")
    with Operations.context(MigrationContext.configure(conn)):
        pl1.downgrade()
        pl1.downgrade()
        assert _x(conn, "SELECT to_regclass('payment_link')").scalar() is None
        pl1.upgrade()
        pl1.upgrade()
    assert _x(conn, "SELECT to_regclass('payment_link')").scalar() == "payment_link"
