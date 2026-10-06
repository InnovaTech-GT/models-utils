"""vpn1 / na1 / ac1 guardrails, after the transport-axis collapse.

vpn1 and na1 were entirely about `network_access` (`vpn_socks5`, the
`olt` -> `outbound` kind rename) and `tr1_transport_axis` dropped that table, so
their model-side constants are gone and only their CHAIN POSITIONS are still
assertable — revisions are immutable and a fresh database still migrates through
both on its way to tr1.

ac1 is different: its `acs_auth_required` column MOVED to
`provisioning_settings` rather than disappearing, and its other two payloads —
the `uq_acs_registration_serial_no_oui` partial UNIQUE and the
`device_credentials.reveal` permission row — are untouched by this cycle. Those
assertions stay in full.

The chain: iv1_insights_v2 -> vpn1_vpn_socks5 -> na1_kind_outbound ->
ac1_acs_tenant_auth -> tr1_transport_axis.
"""
import importlib.util
import os

from database_utils.models import isp

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

def test_vpn1_migration_chain_position():
    vpn1 = _load_vpn1()
    assert vpn1.revision == "vpn1_vpn_socks5"
    assert vpn1.down_revision == "iv1_insights_v2"
    assert len(vpn1.revision) <= 32


# --------------------------------------------------------------------------
# na1 — the kind rename
# --------------------------------------------------------------------------

def test_na1_migration_chain_position():
    na1 = _load_na1()
    assert na1.revision == "na1_kind_outbound"
    assert na1.down_revision == "vpn1_vpn_socks5"
    assert len(na1.revision) <= 32


def test_the_kind_and_mode_vocabulary_left_with_the_table():
    """tr1 dropped `network_access`, so nothing in the models or schemas may still
    speak of kinds or modes — the axis replaced both."""
    import database_utils.schemas as schemas

    for name in ("NETWORK_ACCESS_KINDS", "NETWORK_ACCESS_MODES", "NAT_MODES",
                 "_NETWORK_ACCESS_KINDS_READ", "NetworkAccess"):
        assert not hasattr(isp, name), name
    for name in ("NetworkAccessBase", "NetworkAccessCreate", "NetworkAccessUpdate",
                 "NetworkAccessOut"):
        assert not hasattr(schemas, name), name


# --------------------------------------------------------------------------
# ac1 — the Capa 3 gate and the reveal permission
# --------------------------------------------------------------------------

def test_the_capa_3_gate_moved_rather_than_disappeared():
    """ac1's `acs_auth_required` is the one thing it added to `network_access` that
    tr1 kept: same name, same NOT NULL default-false semantics (decision 8 — OFF
    means ALLOW), now on the tenant singleton, where `ck_network_access_acs_auth_required`
    ("only meaningful on the acs row") is unnecessary because there is one row."""
    column = isp.ProvisioningSettings.__table__.c["acs_auth_required"]
    assert column.nullable is False
    assert not hasattr(isp, "_NETWORK_ACCESS_ACS_AUTH_CHECK")


def test_ac1_migration_chain_position():
    ac1 = _load_ac1()
    assert ac1.revision == "ac1_acs_tenant_auth"
    assert ac1.down_revision == "na1_kind_outbound"
    assert len(ac1.revision) <= 32


def test_the_rotation_window_still_needs_no_pending_columns():
    """ac1 deliberately shipped no `pending_*` columns: the accept-both window was
    a SECOND DeviceCredential row. tr1 keeps the two-row shape and only makes the
    PAIR explicit (two FKs on provisioning_settings) instead of inferring it from
    `created_at DESC, id DESC`. The credential row itself is unchanged."""
    for name in ("pending_secret_ciphertext", "pending_dek_wrapped",
                 "pending_kek_id", "pending_fingerprint", "pending_started_at"):
        assert not hasattr(isp.DeviceCredential, name), name
    assert not hasattr(isp.DeviceCredential, "network_access_id")
    columns = isp.ProvisioningSettings.__table__.c
    assert columns["cwmp_credential_id"].nullable
    assert columns["cwmp_pending_credential_id"].nullable


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
    """ADMIN is the ONLY role that may hold device_credentials.reveal: no
    ISP_ROLES entry grants it, its action is not 'read' (so VIEWER's
    convergent filter never matches it), and ac1 writes no role_permission
    row of its own."""
    isp_seed = _load_isp_seed()
    row = next(p for p in isp_seed.ISP_PERMISSIONS if p["name"] == "device_credentials.reveal")
    assert row["action"] != "read"
    for role_name, spec in isp_seed.ISP_ROLES.items():
        assert "device_credentials.reveal" not in spec["permissions"], role_name
    source = open(
        os.path.join(_VERSIONS_DIR, "ac1_acs_tenant_auth.py"), encoding="utf-8"
    ).read()
    assert "INSERT INTO role_permission" not in source
