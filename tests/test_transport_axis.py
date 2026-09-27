"""Transport axis guardrails (revision `tr1_transport_axis`).

Three layers, one per section:

  1. the CHECK-fragment strings shared between `database_utils/models/isp.py` and
     the hand-written tr1 migration, duplicated on purpose (the
     nc1a/nc2a/nat1/nat2/nat3/vpn1/na1/ac1 precedent — revisions are immutable,
     models are not, so neither may import the other) and pinned byte-identical
     here, plus tr1's position in the chain;
  2. the DB CHECKs themselves, exercised with raw ORM inserts — the path any
     non-Pydantic caller (an importer, a script, a future router) takes;
  3. `ProvisioningSettingsUpdate`, which mirrors what it can see of the CHECKs so
     the API answers 422 instead of letting an IntegrityError surface as a 500.

This file replaces `test_nat_transport_schemas.py`, `test_nat_zt_pylon_schema.py`
and `test_nat_transport_gateway_check.py`, all three of which validated
`NetworkAccessCreate`/`NetworkAccessUpdate` and the `network_access` CHECKs.
"""
import importlib.util
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import isp
from database_utils.models.isp import ProvisioningSettings
from database_utils.schemas.provisioning_settings import (
    ProvisioningSettingsOut,
    ProvisioningSettingsUpdate,
)

_VERSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _load_tr1():
    spec = importlib.util.spec_from_file_location(
        "tr1_transport_axis", os.path.join(_VERSIONS_DIR, "tr1_transport_axis.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. constants, fragments and the chain
# --------------------------------------------------------------------------

def test_the_axis_value_sets():
    assert isp.DIAL_TARGETS == ("device", "gateway")
    assert isp.PROXY_KINDS == ("none", "socks5")


def test_the_cross_product_vocabulary_is_gone():
    """The point of the refactor: `mode` enumerated the cross product of two
    independent questions, which is why "ZeroTier + managed routes" had no value.
    Nothing may reintroduce it."""
    for name in ("NETWORK_ACCESS_KINDS", "NETWORK_ACCESS_MODES", "NAT_MODES",
                 "NetworkAccess", "_NETWORK_ACCESS_MODE_CHECK",
                 "_NETWORK_ACCESS_KIND_CHECK", "_NETWORK_ACCESS_VPN_CHECK",
                 "_NETWORK_ACCESS_PYLON_CHECK",
                 "_NETWORK_ACCESS_NAT_GATEWAY_CHECK",
                 "_NETWORK_ACCESS_ACS_AUTH_CHECK"):
        assert not hasattr(isp, name), name


def test_check_fragments_cover_every_value():
    for value in isp.DIAL_TARGETS:
        assert f"'{value}'" in isp._PROVISIONING_DIAL_TARGET_CHECK
    for value in isp.PROXY_KINDS:
        assert f"'{value}'" in isp._PROVISIONING_PROXY_KIND_CHECK


def test_tr1_migration_fragments_match_model_fragments():
    tr1 = _load_tr1()
    assert tr1._PROVISIONING_DIAL_TARGET_CHECK == isp._PROVISIONING_DIAL_TARGET_CHECK
    assert tr1._PROVISIONING_PROXY_KIND_CHECK == isp._PROVISIONING_PROXY_KIND_CHECK
    assert tr1._PROVISIONING_PROXY_ADDRESS_CHECK == isp._PROVISIONING_PROXY_ADDRESS_CHECK
    assert tr1._PROVISIONING_GATEWAY_HOST_CHECK == isp._PROVISIONING_GATEWAY_HOST_CHECK
    assert tr1._PROVISIONING_CWMP_PAIR_CHECK == isp._PROVISIONING_CWMP_PAIR_CHECK


def test_tr1_migration_chain_position():
    tr1 = _load_tr1()
    assert tr1.revision == "tr1_transport_axis"
    assert tr1.down_revision == "ac1_acs_tenant_auth"
    assert len(tr1.revision) <= 32


def test_tr1_does_not_create_the_unsafe_pending_check():
    """"No pending without a current" is NOT a CHECK, deliberately: both cwmp FKs
    are ON DELETE SET NULL, so deleting the current credential during a rotation
    window would violate it through a referential action and turn an ordinary
    DELETE /device-credentials/{id} into an IntegrityError surfacing as a 500. The
    invariant is a 409 in backend-erp's router instead."""
    tr1 = _load_tr1()
    # The model and the migration must agree that the pair CHECK is the ONLY cwmp
    # constraint. Scanned over the fragment constants rather than the file, whose
    # docstring names the rejected form in prose.
    fragments = [v for k, v in vars(tr1).items() if k.startswith("_PROVISIONING_")]
    assert not any("cwmp_credential_id IS NOT NULL" in f for f in fragments)
    names = {c.name for c in ProvisioningSettings.__table__.constraints if c.name}
    cwmp = {n for n in names if n.startswith("ck_provisioning_settings_cwmp")}
    assert cwmp == {"ck_provisioning_settings_cwmp_pair"}


def test_tr1_reads_the_binding_column_before_dropping_it():
    """`device_credential.network_access_id` is the ONLY thing identifying which
    credentials were the tenant's ACS Inform pair; once dropped, unrecoverable. The
    backfill must precede the drop in upgrade()'s source order."""
    source = open(
        os.path.join(_VERSIONS_DIR, "tr1_transport_axis.py"), encoding="utf-8"
    ).read()
    body = source[source.index("def upgrade("):source.index("def downgrade(")]
    assert body.index("cwmp_pending_credential_id =") < body.index(
        'op.drop_column("device_credential", "network_access_id")'
    )


def test_tr1_inserts_provisioning_disabled():
    """Canon C6: absence of the row means DISABLED, and `enabled` is a live
    provisioning gate. The fold's INSERT branch is the only branch that runs on a
    tenant that never had a settings row, so writing `true` there would silently
    turn provisioning ON with no operator action."""
    source = open(
        os.path.join(_VERSIONS_DIR, "tr1_transport_axis.py"), encoding="utf-8"
    ).read()
    body = source[source.index("def upgrade("):source.index("def downgrade(")]
    fold = body[body.index("INSERT INTO provisioning_settings"):]
    assert "NOW(), NOW(), company_id, false, NULL," in fold


# --------------------------------------------------------------------------
# 2. the DB CHECKs, via raw ORM inserts
# --------------------------------------------------------------------------

def _raw(db, **kwargs):
    row = ProvisioningSettings(id=uuid.uuid4(), company_id=uuid.uuid4(), **kwargs)
    db.add(row)
    db.commit()
    return row


def test_db_check_rejects_an_unknown_dial_target(db):
    with pytest.raises(IntegrityError):
        _raw(db, dial_target="carrier_pigeon")


def test_db_check_rejects_an_unknown_proxy_kind(db):
    with pytest.raises(IntegrityError):
        _raw(db, proxy_kind="wireguard")


def test_db_check_rejects_socks5_with_no_proxy_address(db):
    with pytest.raises(IntegrityError):
        _raw(db, proxy_kind="socks5", proxy_address=None)


def test_db_check_rejects_a_gateway_dial_with_no_gateway_host(db):
    with pytest.raises(IntegrityError):
        _raw(db, dial_target="gateway", gateway_host=None)


def test_db_check_allows_every_legal_combination(db):
    _raw(db, dial_target="device", proxy_kind="none")
    _raw(db, dial_target="gateway", proxy_kind="none", gateway_host="200.9.9.9")
    _raw(db, dial_target="gateway", proxy_kind="socks5", gateway_host="10.147.3.1",
         proxy_address="pylon-a.railway.internal:1080")
    _raw(db, dial_target="device", proxy_kind="socks5",
         proxy_address="hub.example:1080")  # does not raise


def test_db_check_rejects_the_same_credential_as_current_and_pending(db):
    """The rotation window is two DISTINCT secrets; pointing both FKs at one row
    would make "accept current OR pending" a no-op that silently narrows the
    window to nothing."""
    credential_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        _raw(db, cwmp_credential_id=credential_id,
             cwmp_pending_credential_id=credential_id)


def test_db_check_allows_a_pending_pointer_with_no_current_one(db):
    """Not an endorsement of that state — it is what a DB referential action
    produces on its own. Both FKs are ON DELETE SET NULL, so deleting the current
    credential mid-window nulls `cwmp_credential_id` while the pending pointer
    stands. A CHECK forbidding it would turn that DELETE into a 500."""
    _raw(db, cwmp_pending_credential_id=uuid.uuid4())  # does not raise


def test_the_defaults_are_the_public_ip_case(db):
    row = _raw(db)
    assert (row.dial_target, row.proxy_kind) == ("device", "none")
    assert row.acs_auth_required is False
    assert row.enabled is False


# --------------------------------------------------------------------------
# 3. ProvisioningSettingsUpdate
# --------------------------------------------------------------------------

def test_update_rejects_an_unknown_dial_target():
    with pytest.raises(ValidationError) as exc:
        ProvisioningSettingsUpdate(dial_target="carrier_pigeon")
    assert "dial_target" in str(exc.value)


def test_update_rejects_an_unknown_proxy_kind():
    with pytest.raises(ValidationError) as exc:
        ProvisioningSettingsUpdate(proxy_kind="wireguard")
    assert "proxy_kind" in str(exc.value)


def test_update_rejects_switching_to_socks5_while_blanking_the_address():
    with pytest.raises(ValidationError) as exc:
        ProvisioningSettingsUpdate(proxy_kind="socks5", proxy_address="")
    assert "proxy_address" in str(exc.value)


def test_update_rejects_switching_to_gateway_while_blanking_the_host():
    with pytest.raises(ValidationError) as exc:
        ProvisioningSettingsUpdate(dial_target="gateway", gateway_host="   ")
    assert "gateway_host" in str(exc.value)


def test_update_allows_switching_the_axis_and_its_operand_together():
    body = ProvisioningSettingsUpdate(
        dial_target="gateway", gateway_host="200.9.9.9",
        proxy_kind="socks5", proxy_address="pylon-a.railway.internal:1080",
    )
    assert (body.gateway_host, body.proxy_address) == (
        "200.9.9.9", "pylon-a.railway.internal:1080",
    )


def test_update_allows_a_mode_only_switch():
    """Legal against a row that already holds the operand, and the schema cannot
    see the row — so demanding the operand here would forbid a legal operation.
    The router is the only layer that sees the merged row; it answers
    GATEWAY_HOST_REQUIRED / PROXY_ADDRESS_REQUIRED."""
    assert ProvisioningSettingsUpdate(proxy_kind="socks5").proxy_address is None
    assert ProvisioningSettingsUpdate(dial_target="gateway").gateway_host is None


def test_acs_base_url_is_structurally_read_only():
    """It is informational only — nothing in code reads it — and the router applies
    Update with a blanket setattr loop, so keeping the field off the write schema
    is what makes read-only structural rather than guard-dependent. An `http://`
    value would turn every CWMP POST into a bodyless GET at Railway's edge, and a
    CPE pointed at a dead URL has no remote fix."""
    assert "acs_base_url" not in ProvisioningSettingsUpdate.model_fields
    assert "acs_base_url" in ProvisioningSettingsOut.model_fields


def test_the_capa_3_gate_and_the_credential_pair_are_on_the_right_schemas():
    # Arming is a write (gated in the router before the setattr loop); the
    # credential pointers are set by the credential flow, never by a settings
    # PATCH, so they are read-only here.
    assert "acs_auth_required" in ProvisioningSettingsUpdate.model_fields
    for name in ("cwmp_credential_id", "cwmp_pending_credential_id"):
        assert name not in ProvisioningSettingsUpdate.model_fields
        assert name in ProvisioningSettingsOut.model_fields
