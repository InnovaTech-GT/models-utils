"""Whole-branch review I3/I4 (2026-08-13): gateway_host required for a NAT
mode is now enforced at three layers — DB CHECK (nat2_gateway_host_check),
NetworkAccessUpdate schema, and the transport resolver's cross-tenant guard
on a caller-supplied `access` row."""
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models.isp import (
    DeviceCategory, DeviceType, InventoryItem, NetworkAccess,
)
from database_utils.schemas.network_access import NetworkAccessUpdate
from database_utils.utils.transport import resolve_endpoint


def test_db_check_rejects_a_nat_row_with_no_gateway_host(db):
    # Bypasses Pydantic entirely — a raw ORM insert, the exact path an
    # UPDATE through NetworkAccessUpdate (or any future non-Pydantic caller)
    # would take before this migration.
    row = NetworkAccess(
        id=uuid.uuid4(), name="bad", kind="olt", mode="nat_public",
        gateway_host=None, company_id=uuid.uuid4(),
    )
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()


def test_db_check_allows_a_nat_row_with_a_gateway_host(db):
    row = NetworkAccess(
        id=uuid.uuid4(), name="ok", kind="olt", mode="nat_public",
        gateway_host="200.9.9.9", company_id=uuid.uuid4(),
    )
    db.add(row)
    db.commit()  # does not raise


def test_update_schema_rejects_mode_flip_to_nat_with_blank_gateway_host():
    with pytest.raises(ValidationError) as exc:
        NetworkAccessUpdate(mode="nat_public", gateway_host="")
    assert "gateway_host" in str(exc.value)


def test_update_schema_allows_mode_flip_to_nat_with_a_gateway_host():
    row = NetworkAccessUpdate(mode="nat_public", gateway_host="200.9.9.9")
    assert row.gateway_host == "200.9.9.9"


def test_update_schema_allows_unrelated_fields_without_gateway_host():
    # No mode change in this payload -> nothing here to contradict; the DB
    # CHECK is what protects the "already NAT, staying NAT" case this schema
    # cannot see (partial update, no DB read).
    row = NetworkAccessUpdate(name="renamed")
    assert row.gateway_host is None


def _item(db, company_id):
    category = DeviceCategory(id=uuid.uuid4(), key="OLT2", name="OLT2", tier="CORE")
    db.add(category)
    db.flush()
    dtype = DeviceType(
        id=uuid.uuid4(), name="dt", category_id=category.id, company_id=company_id,
    )
    db.add(dtype)
    db.flush()
    item = InventoryItem(
        id=uuid.uuid4(), device_type_id=dtype.id, company_id=company_id,
        mgmt_host="10.1.5.37", cli_protocol="ssh", nat_port=2201,
    )
    db.add(item)
    db.commit()
    return item


def test_resolve_endpoint_refuses_an_access_row_from_another_company(db):
    victim_company = uuid.uuid4()
    attacker_company = uuid.uuid4()
    victim_access = NetworkAccess(
        id=uuid.uuid4(), name="victim-gw", kind="olt", mode="nat_public",
        is_default=True, gateway_host="200.9.9.9", company_id=victim_company,
    )
    db.add(victim_access)
    db.commit()

    item = _item(db, attacker_company)
    endpoint, error = resolve_endpoint(
        db, item, attacker_company, default_port=22, access=victim_access,
    )
    assert endpoint is None
    assert error == "TRANSPORT_UNAVAILABLE"
