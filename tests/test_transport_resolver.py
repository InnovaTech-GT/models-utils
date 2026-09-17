"""Transport resolution (spec §9). The resolver is the only place that decides
what a driver dials; the item's own mgmt_host/mgmt_port never change meaning."""
import uuid

import pytest

from database_utils.models.isp import (
    DeviceCategory, DeviceType, InventoryItem, NetworkAccess,
)
from database_utils.utils.transport import resolve_endpoint


def _company_id(db):
    # The base `db` fixture (unlike `plant`) does not seed a Company row, and
    # SQLite here runs with FK enforcement off (see conftest), so a bare UUID
    # is sufficient company scoping for these tests — mirrors how the `plant`
    # fixture itself never inserts a Company row either.
    return uuid.uuid4()


def _access(db, company_id, mode, gateway_host=None, is_default=True, name=None, pylon_socks5=None):
    row = NetworkAccess(
        id=uuid.uuid4(), name=name or f"na-{mode}", kind="olt",
        mode=mode, is_default=is_default, gateway_host=gateway_host,
        pylon_socks5=pylon_socks5, company_id=company_id,
    )
    db.add(row)
    db.commit()
    return row


def _item(db, company_id, mgmt_host="10.1.5.37", mgmt_port=None, nat_port=None):
    category = db.query(DeviceCategory).filter_by(key="OLT").first()
    if category is None:
        category = DeviceCategory(id=uuid.uuid4(), key="OLT", name="OLT", tier="CORE")
        db.add(category)
        db.flush()
    dtype = DeviceType(
        id=uuid.uuid4(), name=f"dt-{uuid.uuid4().hex[:6]}",
        category_id=category.id, company_id=company_id,
    )
    db.add(dtype)
    db.flush()
    item = InventoryItem(
        id=uuid.uuid4(), device_type_id=dtype.id, company_id=company_id,
        mgmt_host=mgmt_host, mgmt_port=mgmt_port, cli_protocol="ssh",
        nat_port=nat_port,
    )
    db.add(item)
    db.commit()
    return item


def test_no_network_access_row_falls_back_to_the_item(db):
    cid = _company_id(db)
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=2222)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy) == ("10.1.5.37", 2222, None)


def test_direct_mode_uses_the_driver_default_port_when_mgmt_port_is_null(db):
    cid = _company_id(db)
    _access(db, cid, "direct")
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=23)
    assert error is None
    assert (endpoint.host, endpoint.port) == ("10.1.5.37", 23)


def test_nat_public_dials_the_gateway_and_the_mapped_port(db):
    cid = _company_id(db)
    _access(db, cid, "nat_public", gateway_host="200.9.9.9")
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=23, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy) == ("200.9.9.9", 2201, None)
    # spec N1: the item is never rewritten.
    assert (item.mgmt_host, item.mgmt_port) == ("10.1.5.37", 23)


def test_nat_zt_carries_the_tenants_own_pylon_proxy(db):
    cid = _company_id(db)
    _access(db, cid, "nat_zt", gateway_host="10.147.3.1", pylon_socks5="127.0.0.1:1080")
    item = _item(db, cid, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy) == (
        "10.147.3.1", 2201, "127.0.0.1:1080",
    )


def test_nat_zt_without_a_provisioned_pylon_fails_closed(db):
    cid = _company_id(db)
    _access(db, cid, "nat_zt", gateway_host="10.147.3.1", pylon_socks5="")
    item = _item(db, cid, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "PYLON_NOT_PROVISIONED"


def test_two_nat_zt_tenants_never_share_a_proxy(db):
    # acceptance criterion 2 of the redesign spec: same LAN range, different
    # tenants, different Pylons — this is the I4 cross-tenant guard, still
    # exercised the same way after the parameter removal.
    cid_a = _company_id(db)
    cid_b = _company_id(db)
    access_a = _access(db, cid_a, "nat_zt", gateway_host="10.147.3.1", pylon_socks5="pylon-a.railway.internal:1080")
    _access(db, cid_b, "nat_zt", gateway_host="10.147.3.1", pylon_socks5="pylon-b.railway.internal:1080")
    item_a = _item(db, cid_a, nat_port=2201)

    endpoint, error = resolve_endpoint(db, item_a, cid_a, default_port=22, access=access_a)
    assert error is None
    assert endpoint.proxy == "pylon-a.railway.internal:1080"

    # cross-tenant access= misuse: passing tenant B's row while resolving for
    # tenant A must still refuse (I4), not silently pick up B's proxy.
    access_b_row = db.query(NetworkAccess).filter_by(company_id=cid_b).first()
    endpoint2, error2 = resolve_endpoint(db, item_a, cid_a, default_port=22, access=access_b_row)
    assert endpoint2 is None


def test_nat_without_a_nat_port_fails_closed(db):
    cid = _company_id(db)
    _access(db, cid, "nat_public", gateway_host="200.9.9.9")
    item = _item(db, cid, nat_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "NAT_MAPPING_NOT_SET"


def test_vpn_mode_with_no_mgmt_host_fails_closed_and_never_falls_through(db):
    # canon R23, rewritten (spec §9 step 4): a non-direct tenant with an
    # unresolvable target is a step failure, never a direct dial.
    cid = _company_id(db)
    _access(db, cid, "vpn")
    item = _item(db, cid, mgmt_host=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "MGMT_HOST_NOT_SET"


def test_only_the_default_olt_row_is_consulted(db):
    cid = _company_id(db)
    _access(db, cid, "direct", is_default=True, name="the-default")
    _access(db, cid, "nat_public", gateway_host="200.9.9.9",
            is_default=False, name="a-stray-row")
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=22, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert endpoint.host == "10.1.5.37"
