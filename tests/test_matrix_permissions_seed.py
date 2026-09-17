"""Figma permission-matrix rows (cfg3_matrix_permissions) seed invariants.

The three new permission rows are declared in ONE file
(rbac_seed.PERMISSIONS_DATA) but granted in ANOTHER with a different shape
(isp_seed.ISP_ROLES — static per-role name lists). Editing only one half is
the easy mistake, and it fails silently: the row exists, no role holds it,
and the matrix cell renders unchecked forever. These assertions are per-role
so a half-edit fails CI (master plan §9.3).

Loaded by file path because seeds/revisions are importable as modules only
inside alembic/env.py's sys.path setup (test_attested_adoption precedent).
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_CFG3_PATH = os.path.join(_HERE, "..", "alembic", "versions", "cfg3_matrix_permissions.py")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")
_RBAC_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "rbac_seed.py")

MATRIX_PERMISSIONS = ("mobile.technician", "mobile.collector", "audit_logs.read")

# role -> the matrix permission it must hold via its static ISP_ROLES list.
EXPECTED_GRANTS = {
    "TECHNICIAN": "mobile.technician",
    "BILLING": "mobile.collector",
}


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg3():
    return _load_module(_CFG3_PATH, "cfg3_matrix_permissions")


def _isp_seed():
    return _load_module(_ISP_SEED_PATH, "isp_seed_matrix_test")


def _rbac_seed():
    return _load_module(_RBAC_SEED_PATH, "rbac_seed_matrix_test")


# --- migration chain ---

def test_migration_chain_position():
    cfg3 = _cfg3()
    assert cfg3.revision == "cfg3_matrix_permissions"
    assert cfg3.down_revision == "cfg2_service_plan_group"


# --- half 1: the rows are declared exactly once, in rbac_seed ---

@pytest.mark.parametrize("name", MATRIX_PERMISSIONS)
def test_row_declared_in_rbac_seed_and_matches_the_revision(name):
    rbac = _rbac_seed()
    cfg3 = _cfg3()
    seed_rows = [p for p in rbac.PERMISSIONS_DATA if p["name"] == name]
    assert len(seed_rows) == 1, f"{name} must be declared exactly once in PERMISSIONS_DATA"
    revision_row = next(p for p in cfg3.PERMISSIONS if p["name"] == name)
    assert seed_rows[0] == revision_row


def test_rows_are_not_duplicated_into_isp_seed():
    # One row, one seed file (master plan §2.5 drops PR 7's isp_seed plan).
    isp = _isp_seed()
    names = {p["name"] for p in isp.ISP_PERMISSIONS}
    assert names.isdisjoint(MATRIX_PERMISSIONS)


# --- half 2: the grants exist in isp_seed's per-role lists ---

@pytest.mark.parametrize("role_name,permission", sorted(EXPECTED_GRANTS.items()))
def test_isp_role_holds_its_matrix_permission(role_name, permission):
    isp = _isp_seed()
    assert permission in isp.ISP_ROLES[role_name]["permissions"], (
        f"{role_name} must hold {permission} — rbac_seed declares the row, "
        f"isp_seed.ISP_ROLES grants it; editing only one file is a silent no-op"
    )


def test_revision_grants_match_the_seed_lists():
    cfg3 = _cfg3()
    assert dict(cfg3.GRANTS) == EXPECTED_GRANTS


def test_matrix_permissions_granted_to_no_other_isp_role():
    # NOC/WAREHOUSE/SUPPORT have no mobile app; a stray grant means someone
    # pasted the line into the wrong list.
    isp = _isp_seed()
    for role_name, spec in isp.ISP_ROLES.items():
        stray = set(spec["permissions"]) & set(MATRIX_PERMISSIONS)
        expected = {EXPECTED_GRANTS[role_name]} if role_name in EXPECTED_GRANTS else set()
        assert stray == expected, f"{role_name} holds unexpected matrix permissions: {stray}"


# --- half 3: ADMIN/MANAGER converge, so they must NOT be excluded (Q3) ---

@pytest.mark.parametrize("name", MATRIX_PERMISSIONS)
def test_manager_is_not_excluded_from_matrix_permissions(name):
    """Q3 resolved: MANAGER keeps audit_logs.read (and both mobile.* rows).

    Convergence grants MANAGER every permission whose resource is not
    roles/permissions/company and whose name is not excluded — 'mobile' and
    'audit_logs' are neither, so the ONLY way to lose this is someone adding
    the name to one of the two exclusion tuples.
    """
    rbac = _rbac_seed()
    isp = _isp_seed()
    assert name not in rbac.MANAGER_EXCLUDED_PERMISSIONS
    assert name not in isp.ADMIN_ONLY_PERMISSIONS


@pytest.mark.parametrize("name", MATRIX_PERMISSIONS)
def test_matrix_permission_resource_is_manager_visible(name):
    rbac = _rbac_seed()
    row = next(p for p in rbac.PERMISSIONS_DATA if p["name"] == name)
    assert row["resource"] not in ("roles", "permissions", "company")
    assert row["name"] == f"{row['resource']}.{row['action']}"
