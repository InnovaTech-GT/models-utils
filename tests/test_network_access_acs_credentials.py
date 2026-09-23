"""Tenant-scoped TR-069 inform credentials on network_access (plan 23
F1.0/F1.1, decided with Ricardo 2026-09-23): acs_username (globally unique,
backend-generated) + acs_password_hash (bcrypt), kind='acs' rows only.

Same guardrail shape as tests/test_nat_transport_constants.py: the CHECK
fragment is duplicated on purpose between database_utils/models/isp.py and the
hand-written nc1d migration (revisions are immutable, models are not, so
neither can import the other), and these tests pin the two copies
byte-identical. The behaviour tests build only the network_access table on
in-memory SQLite, which is enough to exercise its own UNIQUE and CHECK
constraints (SQLite does not enforce the company FK by default)."""
import importlib.util
import os
import uuid

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.exc import IntegrityError

from database_utils.models import isp
from database_utils.models.isp import NetworkAccess

_VERSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _load_nc1d():
    spec = importlib.util.spec_from_file_location(
        "nc1d_acs_tenant_credentials",
        os.path.join(_VERSIONS_DIR, "nc1d_acs_tenant_credentials.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- constants and migration chain -----------------------------------------

def test_acs_credentials_check_fragment_exists():
    assert isp._NETWORK_ACCESS_ACS_CREDENTIALS_CHECK == (
        "kind = 'acs' OR (acs_username IS NULL AND acs_password_hash IS NULL)"
    )


def test_nc1d_migration_fragment_matches_model_fragment():
    nc1d = _load_nc1d()
    assert (
        nc1d._NETWORK_ACCESS_ACS_CREDENTIALS_CHECK
        == isp._NETWORK_ACCESS_ACS_CREDENTIALS_CHECK
    )


def test_nc1d_migration_chain_position():
    nc1d = _load_nc1d()
    assert nc1d.revision == "nc1d_acs_tenant_credentials"
    assert nc1d.down_revision == "iv1_insights_v2"
    # alembic_version.version_num is VARCHAR(32).
    assert len(nc1d.revision) <= 32


# --- model shape -------------------------------------------------------------

def test_credential_columns_are_nullable_strings():
    cols = NetworkAccess.__table__.c
    assert cols.acs_username.nullable is True
    assert cols.acs_password_hash.nullable is True


def test_credential_constraints_are_declared():
    names = {c.name for c in NetworkAccess.__table__.constraints}
    assert "uq_network_access_acs_username" in names
    assert "ck_network_access_acs_credentials_kind" in names


def test_has_acs_password_never_exposes_the_hash():
    row = NetworkAccess(kind="acs", acs_password_hash=None)
    assert row.has_acs_password is False
    row.acs_password_hash = "$2b$12$notarealhashjustatestvalue"
    assert row.has_acs_password is True


# --- DB behaviour (SQLite, network_access table only) ----------------------

@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    NetworkAccess.__table__.create(eng)
    yield eng
    eng.dispose()


def _row(company_id, name, kind, **extra):
    return {"id": uuid.uuid4(), "company_id": company_id, "name": name, "kind": kind, **extra}


def test_acs_rows_without_credentials_never_collide(engine):
    company = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(insert(NetworkAccess.__table__), [
            _row(company, "acs-a", "acs"),
            _row(company, "acs-b", "acs"),
        ])


def test_acs_row_accepts_credentials(engine):
    with engine.begin() as conn:
        conn.execute(insert(NetworkAccess.__table__), [
            _row(uuid.uuid4(), "acs", "acs", acs_username="t-abc", acs_password_hash="h"),
        ])


def test_acs_username_is_unique_across_tenants(engine):
    with engine.begin() as conn:
        conn.execute(insert(NetworkAccess.__table__), [
            _row(uuid.uuid4(), "acs", "acs", acs_username="t-abc", acs_password_hash="h"),
        ])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(NetworkAccess.__table__), [
            _row(uuid.uuid4(), "acs", "acs", acs_username="t-abc", acs_password_hash="h2"),
        ])


def test_olt_row_rejects_credentials(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(NetworkAccess.__table__), [
            _row(uuid.uuid4(), "olt", "olt", acs_username="t-olt"),
        ])


# --- API schemas: credentials are read-only and the hash never serializes ---

def test_out_schema_exposes_username_and_flag_but_never_the_hash():
    from datetime import datetime, timezone

    from database_utils.schemas.network_access import NetworkAccessOut

    now = datetime.now(timezone.utc)
    row = NetworkAccess(
        id=uuid.uuid4(), company_id=uuid.uuid4(), name="acs", kind="acs",
        mode="direct", is_default=False, created_at=now, updated_at=now,
        acs_username="t-abc", acs_password_hash="$2b$12$notarealhashjustatestvalue",
    )
    dumped = NetworkAccessOut.model_validate(row).model_dump()
    assert dumped["acs_username"] == "t-abc"
    assert dumped["has_acs_password"] is True
    assert "acs_password_hash" not in dumped


def test_create_and_update_schemas_do_not_accept_credentials():
    from database_utils.schemas.network_access import (
        NetworkAccessCreate,
        NetworkAccessUpdate,
    )

    for schema in (NetworkAccessCreate, NetworkAccessUpdate):
        for field in ("acs_username", "acs_password", "acs_password_hash"):
            assert field not in schema.model_fields
