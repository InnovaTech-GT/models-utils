"""Brownfield adoption (doc 30, revision ba1_attested_adoption) guardrails.

The client_services.adopt permission is ADMIN-only, enforced at TWO
independent auto-grant sites that cannot import each other (seeds load
standalone inside alembic/env.py; revisions are immutable): isp_seed's
ADMIN/MANAGER inherit loop and rbac_seed's MANAGER seed + convergent
reconciliation. These tests pin the duplicated exclusion constants
subset-equal (nc2a duplicated-fragment precedent) and the migration's
permission entry identical to the seed's, so a drift edit fails CI instead
of silently granting adoption to MANAGER on the next migrate.
"""
import importlib.util
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from database_utils.models import isp
from database_utils.models.isp import ClientService
from database_utils.schemas.client_service import (
    ClientServiceAdoptBulkIn,
    ClientServiceAdoptBulkItem,
    ClientServiceAdoptBulkOut,
    ClientServiceAdoptBulkRowResult,
    ClientServiceAdoptIn,
    ClientServiceBillingUpdate,
    ClientServiceCreate,
    ClientServiceOut,
    ClientServiceUpdate,
)

_HERE = os.path.dirname(__file__)
_BA1_PATH = os.path.join(_HERE, "..", "alembic", "versions", "ba1_attested_adoption.py")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")
_RBAC_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "rbac_seed.py")

_ADOPTION_FIELDS = ("adopted_at", "adopted_by_user_id", "adoption_note", "activation_evidence")


def _load_module(path, name):
    # seeds/revisions are importable as modules only inside alembic/env.py's
    # sys.path setup — load directly by file path (nc2a test precedent).
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ba1():
    return _load_module(_BA1_PATH, "ba1_attested_adoption")


def _load_isp_seed():
    return _load_module(_ISP_SEED_PATH, "isp_seed_under_test")


def _load_rbac_seed():
    return _load_module(_RBAC_SEED_PATH, "rbac_seed_under_test")


# --- migration <-> seed agreement ---

def test_migration_chain_position():
    ba1 = _load_ba1()
    assert ba1.revision == "ba1_attested_adoption"
    assert ba1.down_revision == "t2_grandfather_email_verified"


def test_migration_permission_matches_seed():
    ba1 = _load_ba1()
    isp_seed = _load_isp_seed()
    assert ba1.ADOPT_PERMISSION["name"] == "client_services.adopt"
    seed_entry = next(
        p for p in isp_seed.ISP_PERMISSIONS if p["name"] == "client_services.adopt"
    )
    assert seed_entry["resource"] == "client_services"
    assert seed_entry["action"] == "adopt"
    assert seed_entry == ba1.ADOPT_PERMISSION


def test_admin_only_exclusions_agree():
    isp_seed = _load_isp_seed()
    rbac_seed = _load_rbac_seed()
    # isp_seed's ADMIN-only set must be covered by rbac_seed's MANAGER
    # exclusion, or the convergent step 3 re-grants it on the next migrate.
    assert set(isp_seed.ADMIN_ONLY_PERMISSIONS) <= set(rbac_seed.MANAGER_EXCLUDED_PERMISSIONS)
    assert "client_services.adopt" in isp_seed.ADMIN_ONLY_PERMISSIONS
    assert "client_services.adopt" in rbac_seed.MANAGER_EXCLUDED_PERMISSIONS
    # legacy ADMIN-only keys must never fall out of the exclusion list
    assert "orders.revert_payment" in rbac_seed.MANAGER_EXCLUDED_PERMISSIONS
    assert "payments.refund" in rbac_seed.MANAGER_EXCLUDED_PERMISSIONS


def test_adopt_not_granted_to_isp_base_roles():
    isp_seed = _load_isp_seed()
    for role_name, spec in isp_seed.ISP_ROLES.items():
        assert "client_services.adopt" not in spec["permissions"], role_name


def test_no_grant_copy_source_for_adopt():
    # rbac_seed step 4 copies legacy grants to successor permissions;
    # client_services.adopt must never appear as a copy target.
    with open(_RBAC_SEED_PATH) as f:
        source = f.read()
    assert '"client_services.adopt")' not in source


# --- model surface ---

def test_model_columns_present():
    cols = ClientService.__table__.columns
    assert "adopted_at" in cols and cols["adopted_at"].nullable
    assert "adopted_by_user_id" in cols and cols["adopted_by_user_id"].nullable
    assert "adoption_note" in cols and cols["adoption_note"].nullable
    fk = next(iter(cols["adopted_by_user_id"].foreign_keys))
    assert fk.column.table.name == "user"
    assert fk.ondelete == "SET NULL"


def test_partial_index_present():
    idx = next(
        i for i in ClientService.__table__.indexes if i.name == "ix_client_service_adopted"
    )
    assert [c.name for c in idx.columns] == ["company_id"]
    assert idx.dialect_options["postgresql"]["where"] is not None


def test_install_state_untouched():
    # regression pin: adoption adds NO install state.
    assert isp.INSTALL_STATES == ("NOT_INSTALLED", "IN_PROGRESS", "INSTALLED")


def test_activation_evidence_constants():
    assert isp.ACTIVATION_EVIDENCE_VALUES == ("provisioned", "attested")


# --- schema exposure (migration_source precedent: Out-only, never writable) ---

def test_schema_exposure():
    for f in _ADOPTION_FIELDS:
        assert f in ClientServiceOut.model_fields
        assert f not in ClientServiceCreate.model_fields
        assert f not in ClientServiceUpdate.model_fields
        assert f not in ClientServiceBillingUpdate.model_fields


def test_adopt_in_note_required_non_empty():
    assert ClientServiceAdoptIn(note="  ok  ").note == "ok"
    with pytest.raises(ValidationError):
        ClientServiceAdoptIn(note="")
    with pytest.raises(ValidationError):
        ClientServiceAdoptIn(note="   ")
    assert ClientServiceAdoptIn(note="x").installed_at is None


def test_bulk_shapes():
    with pytest.raises(ValidationError):
        ClientServiceAdoptBulkItem(note="x")  # client_service_id required
    with pytest.raises(ValidationError):
        ClientServiceAdoptBulkItem(client_service_id=uuid4())  # note required
    with pytest.raises(ValidationError):
        ClientServiceAdoptBulkIn(items=[])  # min_length=1
    out = ClientServiceAdoptBulkOut(results=[])
    assert out.adopted_count == 0 and out.error_count == 0
    row = ClientServiceAdoptBulkRowResult(client_service_id=uuid4(), status="adopted")
    assert row.error is None and row.install_state is None
