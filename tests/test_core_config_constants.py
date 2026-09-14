"""Cycle 7 (doc 25 §2) guardrails: the CHECK-fragment strings and constants
shared between database_utils/models/isp.py and the hand-written nc2a
migration are duplicated on purpose (nc1a `_CREDENTIAL_KIND_CHECK` precedent
— revisions are immutable, models are not, so neither can import the other).
These tests pin the two copies byte-identical and the seed's tier data inside
the CHECK-allowed value set, so a drift edit fails CI instead of failing at
`alembic upgrade head` against prod."""
import importlib.util
import os

from database_utils.models import isp

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "nc2a_core_config.py"
)


def _load_nc2a():
    spec = importlib.util.spec_from_file_location("nc2a_core_config", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- constants (doc 25 §2.1/§2.3/§2.5) ---

def test_install_states_constant():
    assert isp.INSTALL_STATES == ("NOT_INSTALLED", "IN_PROGRESS", "INSTALLED")


def test_device_category_tiers_constant():
    assert isp.DEVICE_CATEGORY_TIERS == ("CORE", "EDGE")


def test_cli_protocols_constant():
    assert isp.CLI_PROTOCOLS == ("ssh", "telnet")


def test_check_fragments_cover_their_constants():
    for value in isp.INSTALL_STATES:
        assert f"'{value}'" in isp._INSTALL_STATE_CHECK
    for value in isp.DEVICE_CATEGORY_TIERS:
        assert f"'{value}'" in isp._DEVICE_CATEGORY_TIER_CHECK
    for value in isp.CLI_PROTOCOLS:
        assert f"'{value}'" in isp._CLI_PROTOCOL_CHECK


# --- model <-> migration byte-identity (the nc1a shared-fragment pattern) ---

def test_migration_fragments_match_model_fragments():
    nc2a = _load_nc2a()
    assert nc2a._DEVICE_CATEGORY_TIER_CHECK == isp._DEVICE_CATEGORY_TIER_CHECK
    assert nc2a._CLI_PROTOCOL_CHECK == isp._CLI_PROTOCOL_CHECK
    assert nc2a._INSTALL_STATE_CHECK == isp._INSTALL_STATE_CHECK


def test_migration_chain_position():
    nc2a = _load_nc2a()
    assert nc2a.revision == "nc2a_core_config"
    assert nc2a.down_revision == "nc1b_device_audit_trigger"


def test_migration_tier_backfill_matches_doc25():
    nc2a = _load_nc2a()
    backfill = dict(nc2a._TIER_BACKFILL)
    assert set(backfill["CORE"]) == {"ROUTER", "SWITCH", "OLT"}
    assert set(backfill["EDGE"]) == {"ONU", "CPE_ROUTER", "ACCESS_POINT"}


# --- seed convergence data (alembic/seeds/isp_seed.py) ---

def _seed_categories():
    # seeds are importable as `seeds.*` only inside alembic/env.py's sys.path
    # setup — load the module directly by path, same trick as _load_nc2a.
    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "seeds", "isp_seed.py")
    spec = importlib.util.spec_from_file_location("isp_seed_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DEVICE_CATEGORIES


def test_seed_tiers_are_check_legal_and_match_backfill():
    categories = _seed_categories()
    by_key = {key: tier for key, _name, _sort, tier, _passive in categories}
    assert all(t in (None, "CORE", "EDGE") for t in by_key.values())
    assert {k for k, t in by_key.items() if t == "CORE"} == {"ROUTER", "SWITCH", "OLT"}
    assert {k for k, t in by_key.items() if t == "EDGE"} == {"ONU", "CPE_ROUTER", "ACCESS_POINT"}


def test_seed_onu_display_name_updated():
    categories = _seed_categories()
    names = {key: name for key, name, _sort, _tier, _passive in categories}
    assert names["ONU"] == "ONU / ONT"
