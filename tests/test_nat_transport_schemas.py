"""Schema-level validation for NAT transport (spec §8, §9)."""
import pytest
from pydantic import ValidationError

from database_utils.schemas.network_access import NetworkAccessCreate


def test_nat_mode_requires_gateway_host():
    with pytest.raises(ValidationError) as exc:
        NetworkAccessCreate(name="gw", kind="olt", mode="nat_public")
    assert "gateway_host" in str(exc.value)


def test_nat_mode_accepts_a_hostname():
    row = NetworkAccessCreate(
        name="gw", kind="olt", mode="nat_public", gateway_host="csr.dyndns.example",
    )
    assert row.gateway_host == "csr.dyndns.example"


def test_nat_zt_accepts_an_rfc1918_address():
    # nat_zt targets are ZeroTier addresses and are always private. Any
    # "globally routable" assertion here would reject every nat_zt tenant.
    row = NetworkAccessCreate(
        name="gw", kind="olt", mode="nat_zt", gateway_host="10.147.3.1",
        pylon_socks5="pylon-acme.railway.internal:1080",
    )
    assert row.gateway_host == "10.147.3.1"


def test_non_nat_mode_does_not_require_gateway_host():
    row = NetworkAccessCreate(name="lab", kind="olt", mode="direct")
    assert row.gateway_host is None


def test_unknown_mode_is_still_rejected():
    with pytest.raises(ValidationError):
        NetworkAccessCreate(name="x", kind="olt", mode="nat_carrier_pigeon")
