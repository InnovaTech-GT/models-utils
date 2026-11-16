"""rtg1_run_triggers on a real Postgres at head: provisioningtrigger holds the
four run-trigger labels next to the original three (doc 68 §3.4)."""
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


def test_head_has_the_trigger_labels(conn):
    labels = set(conn.execute(sa.text("SELECT unnest(enum_range(NULL::provisioningtrigger))::text")).scalars())
    assert {"USER", "WORKFLOW", "API", "MCP", "BOT", "AUTO_SUSPENSION", "AUTO_REACTIVATION"} <= labels
