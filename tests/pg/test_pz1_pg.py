"""pz1_provisioning_zone on a real Postgres: the column exists at head
(nullable VARCHAR(64)); downgrade drops it and upgrade re-adds it, both
idempotent. Run against a database already at `alembic upgrade head`; each
test runs in one rolled-back transaction."""
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
    pz1 = load("versions/pz1_provisioning_zone.py", "pz1_provisioning_zone")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(pz1, fn)()


def _column(conn):
    return conn.execute(sa.text(
        "SELECT is_nullable, data_type, character_maximum_length FROM information_schema.columns "
        "WHERE table_name = 'company' AND column_name = 'provisioning_zone'")).first()


def test_column_at_head_and_round_trip(conn):
    assert tuple(_column(conn)) == ("YES", "character varying", 64)
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # idempotent
    assert _column(conn) is None
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert tuple(_column(conn)) == ("YES", "character varying", 64)
