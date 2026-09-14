"""Schema-level validation for the nat_zt redesign (spec 2026-08-17 §4, §7)."""
import pytest
from pydantic import ValidationError

from database_utils.schemas.network_access import NetworkAccessCreate, NetworkAccessUpdate


def test_nat_zt_create_requires_pylon_socks5():
    with pytest.raises(ValidationError) as exc:
        NetworkAccessCreate(name="gw", kind="olt", mode="nat_zt", gateway_host="10.147.3.1")
    assert "pylon_socks5" in str(exc.value)


def test_nat_zt_create_accepts_a_railway_internal_hostname():
    row = NetworkAccessCreate(
        name="gw", kind="olt", mode="nat_zt",
        gateway_host="10.147.3.1", pylon_socks5="pylon-acme.railway.internal:1080",
    )
    assert row.pylon_socks5 == "pylon-acme.railway.internal:1080"


def test_nat_public_does_not_require_pylon_socks5():
    row = NetworkAccessCreate(
        name="gw", kind="olt", mode="nat_public", gateway_host="200.9.9.9",
    )
    assert row.pylon_socks5 is None


def test_nat_zt_update_with_blank_pylon_socks5_is_rejected():
    with pytest.raises(ValidationError):
        NetworkAccessUpdate(mode="nat_zt", pylon_socks5="")


def test_nat_zt_update_leaving_the_mode_alone_does_not_require_pylon_socks5():
    # mirrors the existing gateway_host Update validator's reasoning: the
    # schema can't see the row's current mode, so a partial PATCH that
    # doesn't touch mode at all must not demand pylon_socks5 either — the
    # DB CHECK (ck_network_access_pylon_socks5) is the layer that sees the
    # merged row.
    row = NetworkAccessUpdate(gateway_host="10.147.3.1")
    assert row.pylon_socks5 is None
