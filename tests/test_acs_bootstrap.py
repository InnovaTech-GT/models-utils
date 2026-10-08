"""acs.* bootstrap values (doc 42 §9.7): create_run checks and registration ensure."""
import base64
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from conftest import CO_B

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    AcsDeviceRegistration,
    DeviceCredential,
    ProvisioningJob,
    ProvisioningSettings,
)
from database_utils.utils import crypto
from database_utils.utils.acs_bootstrap import (
    acs_values,
    cr_password,
    mint_cr_credentials,
    reads_acs,
)
from database_utils.utils.provisioning_resolution import ResolutionError
from database_utils.utils.provisioning_runs import create_run, preflight_run

OLT_ACS = {"configuration": [{
    "name": "set-acs", "driver": "telnet",
    "template": "set remote_manage_cfg acs_url {{acs.url}} acl_pswd {{ acs.inform_password }} "
                "user {{acs.cr_username}} pswd {{acs.cr_password}}"}]}


@pytest.fixture()
def keks(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_KEKS", json.dumps({"k1": base64.b64encode(os.urandom(32)).decode()}))
    monkeypatch.setenv("CREDENTIALS_ACTIVE_KEK_ID", "k1")
    monkeypatch.setenv("GENIEACS_CWMP_PUBLIC_URL", "https://acs.example.com/")


def _inform_credential(db, company_id, secret="Inf0rm_pw-xyz"):
    cred = DeviceCredential(id=uuid.uuid4(), company_id=company_id, name="cwmp",
                            kind="HTTP_BASIC", username=f"t-{company_id}")
    cred.secret_ciphertext, cred.dek_wrapped, cred.kek_id = crypto.encrypt_secret(
        secret, company_id, cred.id)
    db.add(cred)
    db.add(ProvisioningSettings(id=uuid.uuid4(), company_id=company_id, cwmp_credential_id=cred.id))
    db.flush()
    return cred


@pytest.fixture()
def acs_plant(db, plant, keks):
    plant.playbooks["olt-activation"].definition = OLT_ACS
    _inform_credential(db, plant.company_id)
    return plant


def _registrations(db):
    return db.execute(sa.select(AcsDeviceRegistration)).scalars().all()


def test_reads_acs():
    assert reads_acs([OLT_ACS]) and not reads_acs([{"template": "{{secret.x}} acs.url"}])


def test_values_from_settings_env_and_minted_registration(db, acs_plant):
    values = acs_values(db, acs_plant.company_id, " ont-1 ", item_id=acs_plant.cpe.id, ensure=True)
    (reg,) = _registrations(db)
    assert reg.serial_number == "ONT-1" and reg.inventory_item_id == acs_plant.cpe.id
    assert values == {"url": "https://acs.example.com/", "inform_password": "Inf0rm_pw-xyz",
                      "cr_username": "cr-ont-1", "cr_password": cr_password(reg)}
    # a second call reuses the minted pair
    assert acs_values(db, acs_plant.company_id, "ONT-1") == values


def test_acs_base_url_wins_over_env(db, acs_plant):
    db.query(ProvisioningSettings).one().acs_base_url = "http://acs.tenant.net:7547/"
    assert acs_values(db, acs_plant.company_id, "ONT-1")["url"] == "http://acs.tenant.net:7547/"


def test_without_ensure_nothing_is_written(db, acs_plant):
    values = acs_values(db, acs_plant.company_id, "ONT-1")
    assert set(values) == {"url", "inform_password"} and _registrations(db) == []


@pytest.mark.parametrize("breakage", ["no_url", "no_credential"])
def test_not_configured(db, acs_plant, monkeypatch, breakage):
    if breakage == "no_url":
        monkeypatch.delenv("GENIEACS_CWMP_PUBLIC_URL")
    else:
        db.query(ProvisioningSettings).one().cwmp_credential_id = None
    with pytest.raises(ResolutionError) as exc:
        acs_values(db, acs_plant.company_id, "ONT-1", ensure=True)
    assert exc.value.code == "ACS_NOT_CONFIGURED" and _registrations(db) == []


def test_unsafe_value(db, acs_plant, monkeypatch):
    monkeypatch.setenv("GENIEACS_CWMP_PUBLIC_URL", "https://acs.example.com/a b")
    with pytest.raises(ResolutionError) as exc:
        acs_values(db, acs_plant.company_id, "ONT-1")
    assert exc.value.code == "ACS_VALUE_UNSAFE"


@pytest.mark.parametrize("owner", [CO_B, None])
def test_serial_claimed_elsewhere(db, acs_plant, owner):
    db.add(AcsDeviceRegistration(company_id=owner, serial_number="ONT-1", oui="00AABB"))
    db.flush()
    with pytest.raises(ResolutionError) as exc:
        acs_values(db, acs_plant.company_id, "ont-1", ensure=True)
    assert exc.value.code == "ACS_SERIAL_CLAIMED"


def test_mint_is_shared_and_decryptable(db, keks):
    reg = AcsDeviceRegistration(id=uuid.uuid4(), company_id=uuid.uuid4(), serial_number="ABC1")
    password = mint_cr_credentials(reg)
    assert reg.cwmp_cr_username == "cr-abc1" and cr_password(reg) == password


def test_create_run_ensures_registration_before_children(db, acs_plant):
    run = create_run(db, acs_plant.service, PURPOSE_ACTIVATION)
    (reg,) = _registrations(db)
    assert reg.company_id == acs_plant.company_id and reg.cwmp_cr_secret_ciphertext
    frames = json.dumps(run.frames)
    assert cr_password(reg) not in frames and "Inf0rm_pw-xyz" not in frames


def test_create_run_refuses_before_any_child(db, acs_plant, monkeypatch):
    monkeypatch.delenv("GENIEACS_CWMP_PUBLIC_URL")
    with pytest.raises(ResolutionError) as exc:
        create_run(db, acs_plant.service, PURPOSE_ACTIVATION)
    assert exc.value.code == "ACS_NOT_CONFIGURED"
    assert db.execute(sa.select(sa.func.count()).select_from(ProvisioningJob)).scalar() == 0


def test_dry_run_and_preflight_write_no_registration(db, acs_plant):
    create_run(db, acs_plant.service, PURPOSE_ACTIVATION, dry_run=True)
    from database_utils.utils.provisioning_resolution import resolve_provisioning
    preflight_run(db, acs_plant.service, resolve_provisioning(db, acs_plant.service, PURPOSE_ACTIVATION),
                  PURPOSE_ACTIVATION)
    assert _registrations(db) == []


def test_playbooks_without_acs_tokens_never_check(db, plant, monkeypatch):
    monkeypatch.delenv("GENIEACS_CWMP_PUBLIC_URL", raising=False)
    create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert _registrations(db) == []


def test_preflight_raises_create_run_conflicts(db, plant, monkeypatch):
    from database_utils.utils.provisioning_resolution import resolve_provisioning
    monkeypatch.delenv("CREDENTIALS_KEKS", raising=False)
    plant.playbooks["onu-activation"].definition = {
        "secrets": [{"key": "wifi_key", "length": 12}],
        "configuration": [{"name": "s", "driver": "simulator", "template": "{{secret.wifi_key}}"}]}
    db.flush()
    resolved = resolve_provisioning(db, plant.service, PURPOSE_ACTIVATION)
    with pytest.raises(ResolutionError) as exc:
        preflight_run(db, plant.service, resolved, PURPOSE_ACTIVATION)
    assert exc.value.code == "SECRETS_KEY_UNAVAILABLE"
    preflight_run(db, plant.service, resolved, PURPOSE_ACTIVATION, dry_run=True)  # no KEK needed
