"""zt3_ztp_always_on (founder 2026-10-09): ZTP is always on, so the zt1
tenant switch `provisioning_settings.ztp_enabled` is gone from the model, the
schemas and the database. Safety stays with the provisioning gates."""
import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models.isp import ProvisioningSettings
from database_utils.schemas.provisioning_settings import (
    ProvisioningSettingsOut,
    ProvisioningSettingsUpdate,
)


def zt3():
    return load("versions/zt3_ztp_always_on.py", "zt3_ztp_always_on")


def test_zt3_chain():
    m = zt3()
    assert m.revision == "zt3_ztp_always_on" and len(m.revision) <= 32
    assert m.down_revision == "zm1_manual_step"


def test_model_has_no_ztp_switch():
    assert "ztp_enabled" not in ProvisioningSettings.__table__.c


def test_schemas_have_no_ztp_switch():
    assert "ztp_enabled" not in ProvisioningSettingsUpdate.model_fields
    assert "ztp_enabled" not in ProvisioningSettingsOut.model_fields
    # An old client still sending it is ignored, not a 422 and not applied.
    assert ProvisioningSettingsUpdate(ztp_enabled=True).model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError):
        ProvisioningSettingsUpdate(enabled=None)
