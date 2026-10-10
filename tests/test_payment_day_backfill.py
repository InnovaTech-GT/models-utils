"""pd2_payment_day_backfill: chain position and constants. The backfill SQL
itself runs against Postgres in tests/pg/test_payment_day_backfill_pg.py."""
from _mi_helpers import load


def _pd2():
    return load("versions/pd2_payment_day_backfill.py", "pd2_payment_day_backfill")


def test_chain_position():
    mod = _pd2()
    assert mod.revision == "pd2_payment_day_backfill"
    assert mod.down_revision == "zm1_manual_step"


def test_default_day_and_timezone():
    mod = _pd2()
    assert mod.DEFAULT_DAY == 15
    assert mod.TIMEZONE == "America/Guatemala"


def test_backfill_only_touches_null_rows():
    sql = _pd2().FROM_HISTORY
    assert sql.count("payment_day IS NULL") == 2
    assert "reverses_payment_id" in sql
