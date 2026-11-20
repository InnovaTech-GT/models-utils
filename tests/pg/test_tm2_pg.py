"""tm2_drop_pppoe_username on a real Postgres: the column and its index are gone
at head, and downgrade/upgrade round-trips."""
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

COL = ("SELECT count(*) FROM information_schema.columns WHERE table_name = 'client_service' "
       "AND column_name = 'pppoe_username'")
IDX = "SELECT to_regclass('uq_client_service_pppoe')"


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def test_column_and_index_are_gone_and_round_trip(conn):
    assert conn.execute(sa.text(COL)).scalar() == 0
    assert conn.execute(sa.text(IDX)).scalar() is None
    tm2 = load("versions/tm2_drop_pppoe_username.py", "tm2_drop_pppoe_username")
    with Operations.context(MigrationContext.configure(conn)):
        tm2.downgrade()
        tm2.downgrade()
        assert conn.execute(sa.text(COL)).scalar() == 1
        assert conn.execute(sa.text(IDX)).scalar() is not None
        tm2.upgrade()
        tm2.upgrade()
    assert conn.execute(sa.text(COL)).scalar() == 0
