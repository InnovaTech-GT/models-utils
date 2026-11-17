"""cdn1_credit_detail_null on a real Postgres: a JSON 'null' becomes SQL NULL, a real
credit list is untouched, and a rerun is a no-op. Runs inside one rolled-back transaction."""
import os

import pytest
import sqlalchemy as sa
from _mi_helpers import load

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


def test_json_null_becomes_sql_null_and_lists_stay(conn):
    m = load("versions/cdn1_credit_detail_null.py", "cdn1_credit_detail_null")
    conn.execute(sa.text("CREATE TEMP TABLE probe (k text, credit_detail json)"))
    conn.execute(sa.text("INSERT INTO probe VALUES ('n', 'null'::json), ('l', '[{\"cents\": 1}]'::json), ('s', NULL)"))
    stmt = m.STATEMENT.replace("billing_invoice", "probe")
    for _ in range(2):  # rerun is a no-op
        conn.execute(sa.text(stmt))
        rows = dict(conn.execute(sa.text("SELECT k, credit_detail IS NULL FROM probe")).all())
        assert rows == {"n": True, "l": False, "s": True}
    # and the real statement parses against the migrated table
    conn.execute(sa.text(m.STATEMENT))
