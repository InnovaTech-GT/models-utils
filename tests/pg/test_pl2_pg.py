"""pl2_payment_link_support on a real Postgres at head: user.is_system exists
with default false, a system account can never be a super admin, and
uploadedfileownertype holds PAYMENT_LINK. Each test runs in one rolled-back
transaction."""
import os
import uuid

import pytest
import sqlalchemy as sa

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


def _user(conn, **kw):
    row = {"id": uuid.uuid4(), "email": f"{uuid.uuid4().hex}@pl2.gt", "sa": False}
    row.update(kw)
    cols = "id, created_at, name, email, age, password_hash, active, email_verified, is_super_admin"
    vals = ":id, now(), 'u', :email, 30, '!', true, false, :sa"
    if "sys" in row:
        cols += ", is_system"
        vals += ", :sys"
    _x(conn, f'INSERT INTO "user" ({cols}) VALUES ({vals})', **row)
    return row["id"]


def test_is_system_defaults_to_false(conn):
    uid = _user(conn)
    assert _x(conn, 'SELECT is_system FROM "user" WHERE id = :id', id=uid).scalar() is False


def test_a_system_account_is_never_a_super_admin(conn):
    _user(conn, sys=True)
    with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
        _user(conn, sys=True, sa=True)


def test_payment_link_owner_label_exists(conn):
    labels = set(_x(conn, "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                          "WHERE t.typname = 'uploadedfileownertype'").scalars())
    assert {"PAYMENT", "PAYMENT_LINK"} <= labels
