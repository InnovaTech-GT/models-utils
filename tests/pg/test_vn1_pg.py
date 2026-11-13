"""vn1_visit_no_money on a real Postgres, at `alembic upgrade head`: the
collectionvisitcode enum carries the `noMoney` label (doc 68 §2)."""
import os

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


def test_no_money_is_a_collectionvisitcode_label(conn):
    labels = conn.execute(sa.text("SELECT enum_range(NULL::collectionvisitcode)::text[]")).scalar()
    assert "noMoney" in labels
