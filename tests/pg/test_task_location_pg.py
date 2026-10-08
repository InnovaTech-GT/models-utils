"""tl1_task_location on a real Postgres (doc 46 §8): the CHECK and down/up.

Run against a database already at `alembic upgrade head`. Each test runs in
one rolled-back transaction.
"""
import os

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


def _run(conn, fn):
    tl1 = load("versions/tl1_task_location.py", "tl1_task_location")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(tl1, fn)()


def _check(conn, lat, lng):
    """Would ck_task_location accept this pair? Evaluated in the database."""
    expr = conn.execute(sa.text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_task_location'"
    )).scalar()
    assert expr is not None
    body = expr.removeprefix("CHECK ")
    # CHECK semantics: only FALSE rejects; NULL (unknown) passes.
    return conn.execute(sa.text(
        f"SELECT ({body}) IS NOT FALSE FROM (SELECT CAST(:lat AS float8) AS latitude, CAST(:lng AS float8) AS longitude) t"
    ), {"lat": lat, "lng": lng}).scalar()


@pytest.mark.parametrize("lat,lng,ok", [
    (None, None, True),
    (14.55, -90.73, True),
    (14.55, None, False),
    (None, -90.73, False),
    (91, 0, False),
    (0, 181, False),
])
def test_check_accepts_pairs_only_in_range(conn, lat, lng, ok):
    assert _check(conn, lat, lng) is ok


def test_down_then_up_is_clean_and_idempotent(conn):
    _run(conn, "downgrade")
    cols = conn.execute(sa.text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'task' AND column_name IN ('latitude', 'longitude')"
    )).scalar()
    assert cols == 0
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _check(conn, 1, 1) is True
