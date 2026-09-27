"""Transport resolution (spec §9, revision `tr1_transport_axis`). The resolver is
the only place that decides what a driver dials; the item's own
mgmt_host/mgmt_port never change meaning.

The configuration is two orthogonal fields on the tenant's `provisioning_settings`
singleton, so the operator scenarios are the four (five, counting the one the old
`mode` enum could not express) combinations of `dial_target` x `proxy_kind`.
"""
import uuid

from database_utils.models.isp import (
    DeviceCategory, DeviceType, InventoryItem, ProvisioningSettings,
)
from database_utils.utils.transport import (
    company_provisioning_settings,
    resolve_endpoint,
)


def _company_id(db):
    # The base `db` fixture (unlike `plant`) does not seed a Company row, and
    # SQLite here runs with FK enforcement off (see conftest), so a bare UUID
    # is sufficient company scoping for these tests — mirrors how the `plant`
    # fixture itself never inserts a Company row either.
    return uuid.uuid4()


def _settings(db, company_id, dial_target="device", proxy_kind="none",
              proxy_address=None, gateway_host=None, enabled=True):
    row = ProvisioningSettings(
        id=uuid.uuid4(), company_id=company_id, enabled=enabled,
        dial_target=dial_target, proxy_kind=proxy_kind,
        proxy_address=proxy_address, gateway_host=gateway_host,
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


# --- the five operator scenarios -------------------------------------------

def test_scenario_1_devices_have_public_ips(db):
    """device + none (was mode 'direct'): dial the device's own address, no hop."""
    cid = _company_id(db)
    _settings(db, cid)
    item = _item(db, cid, mgmt_host="200.9.9.37", mgmt_port=2222)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "200.9.9.37", 2222, None, "device",
    )


def test_scenario_2_nat_port_map_to_a_public_ip(db):
    """gateway + none (was 'nat_public'): dial the gateway on the mapped port."""
    cid = _company_id(db)
    _settings(db, cid, dial_target="gateway", gateway_host="200.9.9.9")
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=23, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "200.9.9.9", 2201, None, "gateway",
    )
    # spec N1: the item is never rewritten.
    assert (item.mgmt_host, item.mgmt_port) == ("10.1.5.37", 23)


def test_scenario_3_nat_port_map_via_zerotier(db):
    """gateway + socks5 (was 'nat_zt'): the gateway is itself reached through a
    hop, so both the gateway operands AND the proxy are in play."""
    cid = _company_id(db)
    _settings(db, cid, dial_target="gateway", gateway_host="10.147.3.1",
              proxy_kind="socks5", proxy_address="pylon-a.railway.internal:1080")
    item = _item(db, cid, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "10.147.3.1", 2201, "pylon-a.railway.internal:1080", "gateway",
    )


def test_scenario_4_wireguard_hub_with_managed_routes(db):
    """device + socks5 (was 'vpn'): the hub has a real kernel route into the
    tenant LAN, so the target stays the DEVICE's own address and the proxy is only
    the hop. gateway_host/nat_port are not involved and not required."""
    cid = _company_id(db)
    _settings(db, cid, proxy_kind="socks5", proxy_address="hub.example:1080")
    item = _item(db, cid, mgmt_host="192.168.88.1", mgmt_port=2222, nat_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "192.168.88.1", 2222, "hub.example:1080", "device",
    )
    assert (item.mgmt_host, item.mgmt_port) == ("192.168.88.1", 2222)


def test_scenario_5_zerotier_with_managed_routes(db):
    """device + socks5 again, with a ZeroTier-shaped proxy instead of a WireGuard
    one — the scenario the old `mode` cross-product had NO value for, which is the
    whole reason the axis was split. The resolver does not record or care which
    hub technology it is."""
    cid = _company_id(db)
    _settings(db, cid, proxy_kind="socks5",
              proxy_address="pylon-acme.railway.internal:1080")
    item = _item(db, cid, mgmt_host="10.147.20.8", mgmt_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=23)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "10.147.20.8", 23, "pylon-acme.railway.internal:1080", "device",
    )


# --- fail closed (canon R23) ------------------------------------------------

def test_socks5_with_a_blank_proxy_address_fails_closed(db):
    """The reason `proxy_kind` is a stored intent and not
    `proxy_address IS NOT NULL`: without it, "a hub is intended but its address is
    missing" would be indistinguishable from "no hop needed" and the worker would
    silently dial an RFC1918 address from the Railway container.

    Reachable with ck_provisioning_settings_proxy_address in place because the
    CHECK only demands NOT NULL and an empty string commits."""
    cid = _company_id(db)
    _settings(db, cid, proxy_kind="socks5", proxy_address="")
    item = _item(db, cid, mgmt_host="192.168.88.1")
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "PROXY_NOT_PROVISIONED"


def test_socks5_with_a_blank_proxy_address_fails_closed_on_a_gateway_path_too(db):
    # A NULL here cannot be stored at all (ck_provisioning_settings_proxy_address,
    # pinned in test_transport_axis.py); `''` is the reachable hole, on both paths.
    cid = _company_id(db)
    _settings(db, cid, dial_target="gateway", gateway_host="10.147.3.1",
              proxy_kind="socks5", proxy_address="   ")
    item = _item(db, cid, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "PROXY_NOT_PROVISIONED"


def test_gateway_without_a_gateway_host_fails_closed(db):
    cid = _company_id(db)
    _settings(db, cid, dial_target="gateway", gateway_host="  ")
    item = _item(db, cid, nat_port=2201)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "NAT_MAPPING_NOT_SET"


def test_gateway_without_a_nat_port_fails_closed(db):
    cid = _company_id(db)
    _settings(db, cid, dial_target="gateway", gateway_host="200.9.9.9")
    item = _item(db, cid, nat_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "NAT_MAPPING_NOT_SET"


def test_a_proxied_device_path_never_falls_through_to_a_direct_dial(db):
    """canon R23: a tenant whose path is not plain device-no-proxy and whose
    target is unresolvable is a step failure, never a direct dial from wherever
    the worker happens to be running."""
    cid = _company_id(db)
    _settings(db, cid, proxy_kind="socks5", proxy_address="hub.example:1080")
    item = _item(db, cid, mgmt_host="  ")
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "MGMT_HOST_NOT_SET"


# --- no settings row --------------------------------------------------------

def test_no_provisioning_settings_row_behaves_as_device_no_proxy(db):
    """`provisioning_settings` is lazily created (canon C6), so absence is normal
    and must resolve as the legitimate public-IP case — the old 'direct' default.
    It is not a bypass: a tenant with no row also has provisioning DISABLED, so
    no job reaches a driver at all."""
    cid = _company_id(db)
    assert company_provisioning_settings(db, cid) is None
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=2222)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.port, endpoint.proxy, endpoint.dial_target) == (
        "10.1.5.37", 2222, None, "device",
    )


def test_no_settings_row_with_no_mgmt_host_still_fails_closed(db):
    cid = _company_id(db)
    item = _item(db, cid, mgmt_host=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=22)
    assert endpoint is None
    assert error == "MGMT_HOST_NOT_SET"


def test_the_driver_default_port_is_used_when_mgmt_port_is_null(db):
    cid = _company_id(db)
    _settings(db, cid)
    item = _item(db, cid, mgmt_host="10.1.5.37", mgmt_port=None)
    endpoint, error = resolve_endpoint(db, item, cid, default_port=23)
    assert error is None
    assert (endpoint.host, endpoint.port) == ("10.1.5.37", 23)


def test_only_this_tenants_settings_row_is_consulted(db):
    """`company_id` is UNIQUE, so the singleton lookup cannot pick up a sibling
    tenant's row — the multi-row `is_default` selector that used to be the whole
    story (and the most likely way the old create form went wrong) is gone."""
    cid_a = _company_id(db)
    cid_b = _company_id(db)
    _settings(db, cid_a, dial_target="gateway", gateway_host="200.9.9.9")
    _settings(db, cid_b, proxy_kind="socks5", proxy_address="hub-b.example:1080")
    item_b = _item(db, cid_b, mgmt_host="192.168.88.1")
    endpoint, error = resolve_endpoint(db, item_b, cid_b, default_port=22)
    assert error is None
    assert (endpoint.host, endpoint.proxy) == ("192.168.88.1", "hub-b.example:1080")


# --- the I4 cross-tenant guard ---------------------------------------------

def test_resolve_endpoint_refuses_a_settings_row_from_another_company(db):
    """Whole-branch review I4: the `settings=` passthrough exists so a caller that
    already loaded the row avoids a second query, and it is trusted verbatim.
    Passing the wrong tenant's row would otherwise resolve to that tenant's
    gateway or proxy — a cross-tenant address leak."""
    victim = _company_id(db)
    attacker = _company_id(db)
    victim_settings = _settings(
        db, victim, dial_target="gateway", gateway_host="200.9.9.9",
    )
    item = _item(db, attacker, mgmt_host="10.1.5.37", nat_port=2201)
    endpoint, error = resolve_endpoint(
        db, item, attacker, default_port=22, settings=victim_settings,
    )
    assert endpoint is None
    assert error == "TRANSPORT_UNAVAILABLE"


def test_two_proxied_tenants_never_share_a_hop(db):
    """Acceptance criterion 2 of the original nat_zt redesign, restated on the new
    axis: same LAN range, different tenants, different proxies."""
    cid_a = _company_id(db)
    cid_b = _company_id(db)
    settings_a = _settings(
        db, cid_a, dial_target="gateway", gateway_host="10.147.3.1",
        proxy_kind="socks5", proxy_address="pylon-a.railway.internal:1080",
    )
    settings_b = _settings(
        db, cid_b, dial_target="gateway", gateway_host="10.147.3.1",
        proxy_kind="socks5", proxy_address="pylon-b.railway.internal:1080",
    )
    item_a = _item(db, cid_a, nat_port=2201)

    endpoint, error = resolve_endpoint(
        db, item_a, cid_a, default_port=22, settings=settings_a,
    )
    assert error is None
    assert endpoint.proxy == "pylon-a.railway.internal:1080"

    endpoint2, error2 = resolve_endpoint(
        db, item_a, cid_a, default_port=22, settings=settings_b,
    )
    assert endpoint2 is None
    assert error2 == "TRANSPORT_UNAVAILABLE"
