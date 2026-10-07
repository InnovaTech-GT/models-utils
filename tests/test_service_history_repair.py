"""sh1_service_history_repair: chain position and one-way contract.

The repair logic itself is data-driven SQL against Postgres; it was verified on
a copy of the production snapshot (51 historical services, 163 orders moved,
184 line items fixed, 39 replaced services cancelled, no new billing gaps).
"""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "sh1_service_history_repair.py")


def _load():
    spec = importlib.util.spec_from_file_location("sh1_service_history_repair", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_position():
    mod = _load()
    assert mod.revision == "sh1_service_history_repair"
    assert mod.down_revision == "cr1_cash_review"
    assert mod.MIGRATION_SOURCE == "sh1"


def test_downgrade_is_refused():
    with pytest.raises(NotImplementedError):
        _load().downgrade()


def test_plan_for_price_prefers_unique_then_single_active():
    mod = _load()
    a = {"id": 1, "is_active": True}
    b = {"id": 2, "is_active": False}
    c = {"id": 3, "is_active": True}
    by_price = {("co", 7500): [a], ("co", 18000): [a, b], ("co", 20000): [a, c]}
    assert mod._plan_for_price(by_price, "co", 7500) is a
    assert mod._plan_for_price(by_price, "co", 18000) is a      # one active of two
    assert mod._plan_for_price(by_price, "co", 20000) is None   # ambiguous
    assert mod._plan_for_price(by_price, "co", 17500) is None   # no plan at that price
