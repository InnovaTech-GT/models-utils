"""et1: EDGE telnet identity fields, edge_mgmt_cidr, run warnings (doc 67)."""
import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models.isp import DeviceType, ProvisioningRun, ProvisioningSettings
from database_utils.schemas.inventory import DeviceTypeCreate, DeviceTypeUpdate
from database_utils.schemas.provisioning_settings import (
    ProvisioningSettingsOut, ProvisioningSettingsUpdate,
)

MAC_RX = r"MAC Address\s*:\s*([0-9A-Fa-f]{12})"


def test_et1_revision_chain():
    m = load("versions/et1_edge_telnet.py", "et1_edge_telnet")
    assert m.revision == "et1_edge_telnet" and m.down_revision == "ch1_client_chat"
    assert len(m.revision) <= 32


def test_edge_mgmt_cidr_validation():
    assert ProvisioningSettingsUpdate(edge_mgmt_cidr="10.90.0.0/16").edge_mgmt_cidr == "10.90.0.0/16"
    assert ProvisioningSettingsUpdate(edge_mgmt_cidr="").edge_mgmt_cidr is None
    assert ProvisioningSettingsUpdate(edge_mgmt_cidr=None).edge_mgmt_cidr is None
    for bad in ("10.90.1.0/16", "8.8.8.0/24", "fd00::/8", "nope"):   # non-strict, public, v6, junk
        with pytest.raises(ValidationError):
            ProvisioningSettingsUpdate(edge_mgmt_cidr=bad)
    assert "edge_mgmt_cidr" in ProvisioningSettingsOut.model_fields


def test_device_type_identity_regex_one_group():
    u = DeviceTypeUpdate(identity_command=" show status ", identity_regex=MAC_RX)
    assert u.identity_command == "show status" and u.identity_regex == MAC_RX
    assert DeviceTypeUpdate(identity_command="", identity_regex="").identity_regex is None
    for bad in (r"MAC (\w+) (\w+)", r"(?<=MAC)\w+", r"MAC"):
        with pytest.raises(ValidationError):
            DeviceTypeUpdate(identity_regex=bad)
    with pytest.raises(ValidationError):
        DeviceTypeCreate(name="ONU", identity_regex=r"MAC")
    with pytest.raises(ValidationError):
        DeviceTypeUpdate(identity_command="show\nstatus")


def test_columns_exist():
    assert {"identity_command", "identity_regex"} <= set(DeviceType.__table__.c.keys())
    assert "edge_mgmt_cidr" in ProvisioningSettings.__table__.c
    assert "warnings" in ProvisioningRun.__table__.c
