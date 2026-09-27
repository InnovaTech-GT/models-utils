"""vpn transport + kind rename + Capa 3 gate guardrails.

Same job as tests/test_nat_transport_constants.py, for the three revisions this
cycle adds. The CHECK-fragment strings shared between
database_utils/models/isp.py and the hand-written vpn1/na1/ac1 migrations are
duplicated on purpose (the nc1a/nc2a/nat3 precedent — revisions are immutable,
models are not, so neither can import the other). These tests pin the copies
byte-identical, pin each revision's position in the chain, and pin the ac1
permission row against the two seed files it has to agree with.

The chain this cycle produces:

    iv1_insights_v2 -> vpn1_vpn_socks5 -> na1_kind_outbound -> ac1_acs_tenant_auth
"""
import importlib.util
import os

import pytest

from database_utils.models import isp
from database_utils.schemas.network_access import (
    NetworkAccessCreate,
    NetworkAccessOut,
    NetworkAccessUpdate,
)

_VERSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
_SEEDS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "seeds")


def _load(directory, filename, module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(directory, filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_vpn1():
    return _load(_VERSIONS_DIR, "vpn1_vpn_socks5.py", "vpn1_vpn_socks5")


def _load_na1():
    return _load(_VERSIONS_DIR, "na1_kind_outbound.py", "na1_kind_outbound")


def _load_ac1():
    return _load(_VERSIONS_DIR, "ac1_acs_tenant_auth.py", "ac1_acs_tenant_auth")


def _load_isp_seed():
    return _load(_SEEDS_DIR, "isp_seed.py", "isp_seed_for_vpn_test")


def _load_rbac_seed():
    return _load(_SEEDS_DIR, "rbac_seed.py", "rbac_seed_for_vpn_test")


# --------------------------------------------------------------------------
# vpn1 — the tunnel -> vpn rename
# --------------------------------------------------------------------------

def test_vpn_check_fragment_shape():
    assert isp._NETWORK_ACCESS_VPN_CHECK == "mode != 'vpn' OR vpn_socks5 IS NOT NULL"


def test_vpn1_migration_fragment_matches_model_fragment():
    vpn1 = _load_vpn1()
    assert vpn1._NETWORK_ACCESS_VPN_CHECK == isp._NETWORK_ACCESS_VPN_CHECK


def test_vpn1_migration_chain_position():
    vpn1 = _load_vpn1()
    assert vpn1.revision == "vpn1_vpn_socks5"
    assert vpn1.down_revision == "iv1_insights_v2"
    assert len(vpn1.revision) <= 32


def test_the_tunnel_spelling_is_gone_everywhere():
    """Mario's branch shipped `tunnel_socks5` / _NETWORK_ACCESS_TUNNEL_CHECK.
    'tunnel' is still a legal MODE (reserved for canon C10's edge agent) but it
    owns no column and no CHECK, which is what lets the router keep answering
    for it from _UNSHIPPED_MODES instead of the schema answering first."""
    assert not hasattr(isp, "_NETWORK_ACCESS_TUNNEL_CHECK")
    assert not hasattr(isp.NetworkAccess, "tunnel_socks5")
    assert "tunnel" in isp.NETWORK_ACCESS_MODES
    assert "'tunnel'" not in isp._NETWORK_ACCESS_VPN_CHECK


def test_the_mode_check_needed_no_migration_this_cycle():
    """'vpn' was already legal before this cycle, which is why no revision
    touches ck_network_access_mode — and why vpn1's clamp is a real backfill
    rather than the defensive no-op nat2/nat3 shipped."""
    assert "vpn" in isp.NETWORK_ACCESS_MODES
    assert "'vpn'" in isp._NETWORK_ACCESS_MODE_CHECK


# --------------------------------------------------------------------------
# na1 — the additive kind rename
# --------------------------------------------------------------------------

def test_kind_is_one_set_for_reads_and_writes():
    """There is no tolerant READ set any more: na1 narrowed the CHECK, so no
    stored row can carry a value the write set rejects."""
    assert isp.NETWORK_ACCESS_KINDS == ("acs", "outbound")
    assert not hasattr(isp, "_NETWORK_ACCESS_KINDS_READ")


def test_kind_check_is_swapped_not_widened():
    assert isp._NETWORK_ACCESS_KIND_CHECK == "kind IN ('acs','outbound')"
    for value in isp.NETWORK_ACCESS_KINDS:
        assert f"'{value}'" in isp._NETWORK_ACCESS_KIND_CHECK
    assert "'olt'" not in isp._NETWORK_ACCESS_KIND_CHECK


def test_na1_migration_fragment_matches_model_fragment():
    na1 = _load_na1()
    assert na1._NETWORK_ACCESS_KIND_CHECK == isp._NETWORK_ACCESS_KIND_CHECK
    # and the downgrade target is nc1a's immutable copy, unchanged
    assert na1._OLD_NETWORK_ACCESS_KIND_CHECK == "kind IN ('acs','olt')"


def test_na1_migration_chain_position():
    na1 = _load_na1()
    assert na1.revision == "na1_kind_outbound"
    assert na1.down_revision == "vpn1_vpn_socks5"
    assert len(na1.revision) <= 32


def test_olt_is_rejected_on_every_path():
    """NetworkAccessOut inherits NetworkAccessBase.validate_kind, and the base
    validator is now the single strict write set — so 'olt' is refused on READ
    as well as on WRITE. na1 guarantees no stored row carries it."""
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        NetworkAccessOut(
            id=uuid.uuid4(), company_id=uuid.uuid4(), created_at=now, updated_at=now,
            name="legacy", kind="olt", mode="direct", is_default=True,
        )
    with pytest.raises(ValueError):
        NetworkAccessCreate(name="new", kind="olt", mode="direct")
    with pytest.raises(ValueError):
        NetworkAccessUpdate(kind="olt")
    assert NetworkAccessCreate(name="new", kind="outbound", mode="direct").kind == "outbound"


# --------------------------------------------------------------------------
# ac1 — the Capa 3 gate and the reveal permission
# --------------------------------------------------------------------------

def test_acs_auth_check_fragment_shape():
    assert isp._NETWORK_ACCESS_ACS_AUTH_CHECK == "kind = 'acs' OR acs_auth_required = false"


def test_ac1_migration_fragment_matches_model_fragment():
    ac1 = _load_ac1()
    assert ac1._NETWORK_ACCESS_ACS_AUTH_CHECK == isp._NETWORK_ACCESS_ACS_AUTH_CHECK


def test_ac1_migration_chain_position():
    ac1 = _load_ac1()
    assert ac1.revision == "ac1_acs_tenant_auth"
    assert ac1.down_revision == "na1_kind_outbound"
    assert len(ac1.revision) <= 32


def test_ac1_adds_no_pending_columns():
    """The accept-both rotation window is a SECOND DeviceCredential row, not a
    second column set — and deliberately no partial unique index on
    (company_id, network_access_id), which would forbid that row."""
    for name in ("pending_secret_ciphertext", "pending_dek_wrapped",
                 "pending_kek_id", "pending_fingerprint", "pending_started_at"):
        assert not hasattr(isp.DeviceCredential, name), name
    index_names = {
        c.name for c in isp.DeviceCredential.__table__.constraints
    } | {i.name for i in isp.DeviceCredential.__table__.indexes}
    assert "uq_device_credential_acs_inform" not in index_names


def test_the_null_oui_serial_is_unique():
    """uq_acs_registration_identity is a plain two-column UNIQUE and Postgres
    treats NULLs as distinct, so without this partial index two tenants can
    both claim one serial and the inform-auth lookup picks arbitrarily."""
    index = next(
        i for i in isp.AcsDeviceRegistration.__table__.indexes
        if i.name == "uq_acs_registration_serial_no_oui"
    )
    assert index.unique
    assert [c.name for c in index.columns] == ["serial_number"]
    # sqlite_where must mirror postgresql_where or the test suite's create_all
    # builds a non-partial unique index and rejects every NULL-oui sibling.
    assert index.dialect_options["postgresql"]["where"] is not None
    assert index.dialect_options["sqlite"]["where"] is not None


def test_ac1_permission_matches_the_isp_seed_row():
    ac1 = _load_ac1()
    isp_seed = _load_isp_seed()
    assert [p["name"] for p in ac1.PERMISSIONS] == ["device_credentials.reveal"]
    for perm in ac1.PERMISSIONS:
        seed_entry = next(
            p for p in isp_seed.ISP_PERMISSIONS if p["name"] == perm["name"]
        )
        assert seed_entry == perm
        assert perm["name"] == f"{perm['resource']}.{perm['action']}"


def test_reveal_is_admin_only():
    """isp_seed._seed_permissions cross-joins every ISP_PERMISSIONS name onto
    global ADMIN *and* MANAGER minus ADMIN_ONLY_PERMISSIONS, so dropping the
    name from either tuple silently hands every tenant manager the tenant's ACS
    password. ADMIN is the ONLY role that may hold it: no ISP_ROLES entry grants
    it, and ac1 writes no role_permission row of its own."""
    isp_seed = _load_isp_seed()
    rbac_seed = _load_rbac_seed()
    assert "device_credentials.reveal" in isp_seed.ADMIN_ONLY_PERMISSIONS
    assert "device_credentials.reveal" in rbac_seed.MANAGER_EXCLUDED_PERMISSIONS
    for role_name, spec in isp_seed.ISP_ROLES.items():
        assert "device_credentials.reveal" not in spec["permissions"], role_name
    source = open(
        os.path.join(_VERSIONS_DIR, "ac1_acs_tenant_auth.py"), encoding="utf-8"
    ).read()
    assert "INSERT INTO role_permission" not in source
