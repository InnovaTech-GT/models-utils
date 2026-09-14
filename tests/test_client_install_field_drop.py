"""Client-install-field removal (feature `client-install-field`, revision
cf1_drop_client_install_fields) guardrails: the stored per-client install
state is gone everywhere — model, schemas, workflow field registry, seed
template — and the derived services-summary rollup is Out-only. Plus the
migration-chain pins (c8a/nc2a precedent) and a quote-agnostic single-head
file scan over alembic/versions/ so a parallel-branch second head fails CI
instead of failing `alembic upgrade head` against prod."""
import glob
import importlib.util
import os
import re

from database_utils.models.crm import Client
from database_utils.schemas.client import ClientBase, ClientCreate, ClientOut, ClientUpdate
from database_utils.utils import workflow_fields

_HERE = os.path.dirname(__file__)
_VERSIONS_DIR = os.path.join(_HERE, "..", "alembic", "versions")
_CF1_PATH = os.path.join(_VERSIONS_DIR, "cf1_drop_client_install_fields.py")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")

_DROPPED_FIELDS = ("installation_status", "installation_date")


def _load_module(path, name):
    # revisions/seeds are importable as modules only inside alembic/env.py's
    # sys.path setup — load directly by file path (nc2a test precedent).
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cf1():
    return _load_module(_CF1_PATH, "cf1_drop_client_install_fields")


def _load_isp_seed():
    return _load_module(_ISP_SEED_PATH, "isp_seed_under_test")


# --- migration chain ---

def test_migration_chain_position():
    cf1 = _load_cf1()
    assert cf1.revision == "cf1_drop_client_install_fields"
    assert cf1.down_revision == "ba1_attested_adoption"


def test_migration_dropped_constants():
    cf1 = _load_cf1()
    assert cf1._DROPPED_COLUMNS == ("installation_status", "installation_date")
    # exact PG type name created by cd2f0076c709
    assert cf1._DROPPED_ENUM == "installationstatus"


def test_single_head_file_scan():
    """Quote-agnostic scan of every revision file: exactly ONE head.

    The head's NAME is deliberately not asserted — pinning it made this guard
    fail on every subsequent revision, which taught people to edit the guard
    rather than read it. The invariant worth protecting is that the migration
    graph never branches."""
    revision_re = re.compile(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
    down_re = re.compile(r"^down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", re.M)
    revisions, parents = set(), set()
    for path in glob.glob(os.path.join(_VERSIONS_DIR, "*.py")):
        source = open(path).read()
        rev = revision_re.search(source)
        assert rev, f"no revision id found in {os.path.basename(path)}"
        revisions.add(rev.group(1))
        down = down_re.search(source)
        if down:  # the root revision has down_revision = None
            parents.add(down.group(1))
    heads = revisions - parents
    assert len(heads) == 1, f"migration graph has branched: {sorted(heads)}"


# --- model surface ---

def test_client_model_columns_dropped():
    cols = Client.__table__.columns
    for field in _DROPPED_FIELDS:
        assert field not in cols
    # the neighbor ISP field stays
    assert "service_availability" in cols


def test_installation_status_enum_gone():
    from database_utils.models import crm
    assert not hasattr(crm, "InstallationStatus")


# --- schema surface ---

def test_schemas_dropped_fields():
    for model in (ClientBase, ClientCreate, ClientUpdate, ClientOut):
        for field in _DROPPED_FIELDS:
            assert field not in model.model_fields, (model.__name__, field)


def test_client_out_services_rollup():
    # Out-only, backend-computed, always present with a 0 default.
    for field in ("services_total", "services_installed"):
        assert field in ClientOut.model_fields
        assert ClientOut.model_fields[field].is_required() is False
        assert field not in ClientCreate.model_fields
        assert field not in ClientUpdate.model_fields
    out = ClientOut(
        id="00000000-0000-0000-0000-000000000001",
        company_id="00000000-0000-0000-0000-000000000002",
        name="x", tax_id=None, address=None, phone=None, email=None,
        contact=None, observations=None, advisor_id=None,
    )
    assert out.services_total == 0 and out.services_installed == 0


# --- workflow field registry ---

def test_workflow_fields_client_registry():
    names = {f["name"] for f in workflow_fields.RESOURCE_FIELDS["client"]}
    for field in _DROPPED_FIELDS:
        assert field not in names
    assert "service_availability" in names


# --- seed template (new-installation v3) ---

def test_seed_new_installation_template():
    seed = _load_isp_seed()
    tpl = next(t for t in seed.WORKFLOW_TEMPLATES if t["key"] == "new-installation")
    refs = {step["ref"] for step in tpl["definition"]["steps"]}
    assert refs == {"s1", "s2"}  # s3 (client install-status cache sync) gone
    assert tpl["definition"]["edges"] == [{"from": "s1", "to": "s2"}]  # s2 terminal


def test_seed_has_no_dropped_field_references():
    seed = _load_isp_seed()
    import json
    blob = json.dumps(seed.WORKFLOW_TEMPLATES)
    for field in _DROPPED_FIELDS:
        assert field not in blob
