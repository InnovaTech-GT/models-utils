"""cdn1_credit_detail_null (6.34.1): chain and data-only shape. The UPDATE runs on a
real Postgres in tests/pg/test_cdn1_pg.py."""
import inspect
import re

from _mi_helpers import load


def cdn1():
    return load("versions/cdn1_credit_detail_null.py", "cdn1_credit_detail_null")


def test_cdn1_revision_chain():
    m = cdn1()
    assert m.revision == "cdn1_credit_detail_null" and m.down_revision == "rtg2_run_trigger_backfill"
    assert len(m.revision) <= 32


def test_cdn1_is_data_only():
    m = cdn1()
    assert not re.search(r"\b(ALTER|CREATE|DROP)\b", m.STATEMENT)
    assert "lock_timeout = '5s'" in inspect.getsource(m.upgrade)
