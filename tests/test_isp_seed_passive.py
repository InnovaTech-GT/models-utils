"""Passive classification converges once and never reverts an admin edit."""

import importlib.util
import pathlib
import sys

PASSIVE = {"SPLITTER", "SPLICE_CLOSURE", "PATCH_PANEL", "ANTENNA"}
SRC = pathlib.Path(__file__).parent.parent / "alembic" / "seeds" / "isp_seed.py"


def _seed_module():
    """The seed is importable as `seeds.isp_seed` only under alembic's env.py,
    which puts the alembic dir on sys.path. Load it by path instead."""
    sys.path.insert(0, str(SRC.parent.parent))
    spec = importlib.util.spec_from_file_location("isp_seed_for_tests", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEVICE_CATEGORIES = _seed_module().DEVICE_CATEGORIES


def test_passive_categories_are_declared():
    by_key = {row[0]: row for row in DEVICE_CATEGORIES}
    for key in PASSIVE:
        assert by_key[key][4] is True, f"{key} should seed passive"


def test_active_gear_is_not_marked_passive():
    by_key = {row[0]: row for row in DEVICE_CATEGORIES}
    for key in ("OLT", "ONU", "ROUTER", "SWITCH", "CPE_ROUTER", "ACCESS_POINT"):
        assert by_key[key][4] is False, f"{key} must stay configurable"


def test_ups_and_radio_stay_configurable():
    """A UPS may expose SNMP and a radio is an active link end. Marking either
    passive would silently exclude it from provisioning forever."""
    by_key = {row[0]: row for row in DEVICE_CATEGORIES}
    assert by_key["UPS"][4] is False
    assert by_key["RADIO"][4] is False


def test_every_category_declares_the_flag():
    for row in DEVICE_CATEGORIES:
        assert len(row) == 5, f"{row[0]} is missing is_passive"
        assert isinstance(row[4], bool)


def test_the_classification_is_gated_like_the_tier_backfill():
    """Convergent-once, not every-run: a super-admin who marks a splitter active
    must not have it reverted on the next migrate."""
    body = SRC.read_text()
    assert "SELECT COUNT(*) FROM device_category WHERE is_passive" in body
    assert "if not any_passive:" in body


def test_templates_no_longer_reference_the_retired_config_key():
    body = SRC.read_text()
    assert '"use_topology"' not in body
    assert '"use_service_path": True' in body
