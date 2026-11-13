"""rtg2_run_trigger_backfill (doc 68 §3.4): chain, one-way, held. The data
behaviour is proven on a real Postgres in tests/pg/test_rtg2_pg.py."""
import inspect
import re

from _mi_helpers import load

HEAD_AT_CUT = "rtg1_run_triggers"


def rtg2():
    return load("versions/rtg2_run_trigger_backfill.py", "rtg2_run_trigger_backfill")


def test_rtg2_revision_chain():
    m = rtg2()
    assert m.revision == "rtg2_run_trigger_backfill" and m.down_revision == HEAD_AT_CUT
    assert len(m.revision) <= 32


def test_rtg2_is_data_only_and_the_cron_user_is_the_system_one():
    m = rtg2()
    assert m.CRON_EMAIL == "system-cron@internal.erp"
    src = inspect.getsource(m)
    assert not re.search(r"\b(ALTER|CREATE|DROP)\b", src.split("def upgrade")[0].split('"""', 2)[2])
    assert "lock_timeout = '5s'" in inspect.getsource(m.upgrade)


def test_rtg2_downgrade_is_a_noop():
    assert not re.search(r"\b(UPDATE|INSERT|DELETE|op\.)", inspect.getsource(rtg2().downgrade).split('"""')[0].replace("# ", ""))
