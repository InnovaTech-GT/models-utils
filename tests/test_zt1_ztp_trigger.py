"""zt1_ztp_trigger (doc 43 §4) guardrails: chain position, the CHECK literals
pinned byte-identical to the models (revisions are immutable, models are not),
the new columns and table, and the settings schema's null rejection."""
import uuid

import pytest
from _mi_helpers import load, mi2
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    USER_NOTIFICATION_KINDS,
    UserNotification,
    UserPushToken,
    auth,
)
from database_utils.models.isp import ProvisioningSettings
from database_utils.schemas.provisioning_settings import (
    ProvisioningSettingsOut,
    ProvisioningSettingsUpdate,
)

ZTP_KINDS = ("ZTP_SUCCEEDED", "ZTP_FAILED", "ZTP_NEEDS_ATTENTION", "ZTP_ROLLBACK_INCOMPLETE")


def zt1():
    return load("versions/zt1_ztp_trigger.py", "zt1_ztp_trigger")


def test_chain_position():
    m = zt1()
    assert m.revision == "zt1_ztp_trigger"
    assert m.down_revision == "pe1_playbook_phases"
    assert len(m.revision) <= 32


def test_kind_check_literals():
    m = zt1()
    # zm1 (doc 42d) extended the CHECK after zt1: zt1's literal is zm1's "pre".
    assert m._KIND_CHECK == load("versions/zm1_manual_step.py", "zm1_manual_step")._KIND_CHECK_PRE
    assert m._KIND_CHECK_PRE == mi2().USER_NOTIFICATION_KIND_CHECK
    assert USER_NOTIFICATION_KINDS[3:7] == ZTP_KINDS
    for kind in USER_NOTIFICATION_KINDS:
        assert f"'{kind}'" in auth._USER_NOTIFICATION_KIND_CHECK
        assert len(kind) <= 32


def test_token_check_literals():
    m = zt1()
    assert m._PLATFORM_CHECK == auth._PUSH_PLATFORM_CHECK
    assert m._APP_CHECK == auth._PUSH_APP_CHECK
    assert auth.PUSH_PLATFORMS == ("android", "ios")
    assert auth.PUSH_APPS == ("tecnicos",)


def test_push_pending_index_matches_the_migration():
    idx = next(i for i in UserNotification.__table__.indexes
               if i.name == "ix_user_notification_push_pending")
    assert str(idx.dialect_options["postgresql"]["where"]) == zt1()._PUSH_PENDING_WHERE
    assert [c.name for c in idx.columns] == ["created_at"]


def test_columns():
    ztp = ProvisioningSettings.__table__.c["ztp_enabled"]
    assert ztp.nullable is False
    assert ztp.server_default.arg == "false"
    push = UserNotification.__table__.c["push_state"]
    assert push.nullable is True and push.type.length == 12
    t = UserPushToken.__table__.c
    assert t["token"].unique and t["token"].type.length == 255
    assert not t["token"].nullable and not t["platform"].nullable and not t["app"].nullable
    names = {c.name for c in UserPushToken.__table__.constraints}
    assert {"ck_user_push_token_platform", "ck_user_push_token_app"} <= names


def test_ztp_kinds_and_push_token_constraints(db):
    co, user = uuid.uuid4(), uuid.uuid4()
    for kind in ZTP_KINDS:
        db.add(UserNotification(company_id=co, user_id=user, kind=kind,
                                dedupe_key=f"{kind}:1", push_state="PENDING"))
    db.flush()
    db.add(UserNotification(company_id=co, user_id=user, kind="ZTP_OTHER", dedupe_key="x"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    tok = "ExponentPushToken[abc]"
    db.add(UserPushToken(company_id=co, user_id=user, token=tok, platform="android", app="tecnicos"))
    db.flush()
    db.add(UserPushToken(company_id=co, user_id=uuid.uuid4(), token=tok, platform="ios", app="tecnicos"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    for bad in ({"platform": "web", "app": "tecnicos"}, {"platform": "ios", "app": "cobros"}):
        db.add(UserPushToken(company_id=co, user_id=user, token=f"t-{bad}", **bad))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_settings_update_rejects_explicit_null_on_not_null_fields():
    for field in ("ztp_enabled", "enabled", "acs_auth_required", "dial_target", "proxy_kind"):
        with pytest.raises(ValidationError):
            ProvisioningSettingsUpdate(**{field: None})
    # Omitted is fine, and nullable fields still accept an explicit null.
    assert ProvisioningSettingsUpdate().model_dump(exclude_unset=True) == {}
    assert ProvisioningSettingsUpdate(ztp_enabled=True).model_dump(exclude_unset=True) == {
        "ztp_enabled": True}
    ProvisioningSettingsUpdate(proxy_address=None, gateway_host=None, default_inform_interval=None)


def test_settings_out_defaults_ztp_off():
    assert ProvisioningSettingsOut.model_fields["ztp_enabled"].default is False
