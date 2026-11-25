"""ow1_onu_web (doc 77): CPE_WEB credentials + priority, cpe_web_cidrs and the
onu_config_backup table; the cpe_web allow-list helpers."""
import uuid

import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models import isp
from database_utils.models.isp import DeviceCredential, OnuConfigBackup, ProvisioningSettings
from database_utils.schemas.device_credential import (
    DeviceCredentialCreate, DeviceCredentialOut, DeviceCredentialUpdate,
)
from database_utils.schemas.provisioning_settings import (
    ProvisioningSettingsOut, ProvisioningSettingsUpdate,
)
from database_utils.utils.cpe_web import MAX_CPE_WEB_CIDRS, host_in_any_cidr, normalize_cidrs


def _ow1():
    return load("versions/ow1_onu_web.py", "ow1_onu_web")


def test_revision_chain():
    m = _ow1()
    assert m.revision == "ow1_onu_web" and m.down_revision == "dm1_device_metrics"
    assert len(m.revision) <= 32


def test_check_literals_match_the_model():
    m = _ow1()
    assert "CPE_WEB" in isp.CREDENTIAL_KINDS
    assert m._CREDENTIAL_KIND_CHECK == isp._CREDENTIAL_KIND_CHECK
    assert "'CPE_WEB'" not in m._CREDENTIAL_KIND_CHECK_PRE
    for kind in isp.CREDENTIAL_KINDS:
        assert f"'{kind}'" in isp._CREDENTIAL_KIND_CHECK


def test_columns_and_table():
    assert "priority" in DeviceCredential.__table__.c
    assert "cpe_web_cidrs" in ProvisioningSettings.__table__.c
    t = OnuConfigBackup.__table__
    for col in ("secret_ciphertext", "dek_wrapped", "kek_id"):  # device_credential's envelope
        assert type(t.c[col].type) is type(DeviceCredential.__table__.c[col].type)
        assert not t.c[col].nullable
    ondelete = {fk.parent.name: fk.ondelete for fk in t.foreign_keys}
    assert ondelete == {"company_id": "CASCADE", "inventory_item_id": "CASCADE",
                        "provisioning_run_id": "SET NULL", "provisioning_job_id": "SET NULL",
                        "credential_id": "SET NULL"}
    [ix] = t.indexes
    assert [c.name for c in ix.columns] == ["company_id", "inventory_item_id", "created_at"]


def test_credential_priority_schema():
    base = {"name": "f", "kind": "CPE_WEB", "username": "admin", "secret": "s"}
    assert DeviceCredentialCreate(**base).priority == 0
    assert DeviceCredentialCreate(**base, priority=1000).priority == 1000
    for bad in (-1, 1001):
        with pytest.raises(ValidationError):
            DeviceCredentialCreate(**base, priority=bad)
    assert DeviceCredentialUpdate().priority is None
    assert DeviceCredentialUpdate(priority=5).model_dump(exclude_unset=True) == {"priority": 5}
    with pytest.raises(ValidationError):
        DeviceCredentialUpdate(priority=None)
    assert "priority" in DeviceCredentialOut.model_fields


def test_cpe_web_cidrs_schema_normalizes():
    u = ProvisioningSettingsUpdate(cpe_web_cidrs=" 10.0.0.0/8 , 10.0.0.0/8,FD00:0::/8 ,")
    assert u.cpe_web_cidrs == "10.0.0.0/8,fd00::/8"
    assert ProvisioningSettingsUpdate(cpe_web_cidrs="").cpe_web_cidrs is None
    assert ProvisioningSettingsUpdate(cpe_web_cidrs=" , ").cpe_web_cidrs is None
    assert ProvisioningSettingsUpdate(cpe_web_cidrs=None).cpe_web_cidrs is None
    assert "cpe_web_cidrs" in ProvisioningSettingsOut.model_fields


@pytest.mark.parametrize("bad", [
    "nope", "10.0.0.1/8", "10.0.0.0/33",               # junk, host bits, prefix
    "0.0.0.0/0", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "::/0", "fe80::/10",
    "::1/128", "10.0.0.0/8;192.168.0.0/16",
])
def test_cpe_web_cidrs_schema_rejects(bad):
    with pytest.raises(ValidationError):
        ProvisioningSettingsUpdate(cpe_web_cidrs=bad)


def test_a_bare_address_is_a_single_host_network():
    assert normalize_cidrs("10.0.0.1") == "10.0.0.1/32"


def test_cpe_web_cidrs_cap():
    many = ",".join(f"10.{i}.0.0/16" for i in range(MAX_CPE_WEB_CIDRS))
    assert normalize_cidrs(many).count(",") == MAX_CPE_WEB_CIDRS - 1
    with pytest.raises(ValueError):
        normalize_cidrs(many + ",10.200.0.0/16")


@pytest.mark.parametrize("host,cidrs,ok", [
    ("10.1.2.3", "10.0.0.0/8", True),
    (" 10.70.2.9 ", "192.168.0.0/16,10.70.0.0/16", True),
    ("11.0.0.1", "10.0.0.0/8", False),
    ("fd00::5", "10.0.0.0/8,fd00::/8", True),
    ("::ffff:10.1.1.1", "10.0.0.0/8", True),       # v4-mapped is judged as v4
    ("::ffff:127.0.0.1", "10.0.0.0/8", False),
    ("onu.local", "10.0.0.0/8", False),            # names are never resolved
    ("10.1.2.3", None, False),                     # NULL setting: nothing allowed
    ("10.1.2.3", "", False),
    ("10.1.2.3", "garbage", False),                # unparsable: fail closed
    (None, "10.0.0.0/8", False),
])
def test_host_in_any_cidr(host, cidrs, ok):
    assert host_in_any_cidr(host, cidrs) is ok


def test_backup_row_round_trips(db, plant):
    row = OnuConfigBackup(company_id=plant.company_id, inventory_item_id=plant.cpe.id,
                          sha256="0" * 64, size_bytes=10, secret_ciphertext=b"x",
                          dek_wrapped=b"y", kek_id="k")
    db.add(row)
    db.flush()
    assert row.id and row.created_at and row.provisioning_run_id is None
