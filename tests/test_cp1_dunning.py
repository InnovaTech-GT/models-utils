"""cp1: dunning settings schema, working_days, new models, RBAC pins."""
import pytest
from _mi_helpers import isp_seed, load
from pydantic import ValidationError

from database_utils.constants.roles import Roles
from database_utils.models import (
    Company, DunningRun, DunningRunOutcome, PaymentPromise, PaymentPromiseSource,
    PaymentPromiseStatus,
)
from database_utils.schemas.company import (
    DEFAULT_WORKING_DAYS, CompanyOut, CompanyUpdate, DunningSettings, MobileSettings,
)


def _cp1():
    return load("versions/cp1_dunning.py", "cp1_test")


def test_chain_position():
    assert _cp1().down_revision == "wb1_whatsapp_bot_config"
    assert len(_cp1().revision) <= 32


def test_defaults_are_off():
    s = DunningSettings()
    assert s.auto_suspension_mode == "OFF" and not s.captive_portal_enabled
    assert not s.promise_reconnect_enabled and s.min_overdue_orders >= 1


@pytest.mark.parametrize("bad", [
    {"min_overdue_orders": 0},
    {"promise_capacity_pct": -1},
    {"promise_capacity_pct": 101},
    {"promise_window_days": -1},
    {"auto_suspension_mode": "MAYBE"},
    {"unknown": 1},
    {"promise_reconnect_enabled": True},
    {"promise_reconnect_enabled": True, "auto_suspension_mode": "REPORT"},
])
def test_dunning_rejects(bad):
    with pytest.raises(ValidationError):
        DunningSettings(**bad)


def test_dunning_accepts_boundaries_and_reconnect_with_on():
    s = DunningSettings(auto_suspension_mode="ON", promise_reconnect_enabled=True,
                        min_overdue_orders=1, promise_capacity_pct=100, promise_window_days=0,
                        portal_contact={"whatsapp": "502", "message": "hola"})
    assert s.portal_contact.whatsapp == "502"
    DunningSettings(promise_capacity_pct=0)


@pytest.mark.parametrize("bad", [[], [0, 0], [7], [-1]])
def test_working_days_rejects(bad):
    with pytest.raises(ValidationError):
        MobileSettings(working_days=bad)


def test_working_days_default_and_sorted():
    assert MobileSettings().working_days is None
    assert DEFAULT_WORKING_DAYS == [0, 1, 2, 3, 4, 5]
    assert MobileSettings(working_days=[5, 0, 2]).working_days == [0, 2, 5]


def test_company_schemas_carry_dunning_settings():
    upd = CompanyUpdate(dunning_settings={"auto_suspension_mode": "REPORT"})
    assert upd.dunning_settings.auto_suspension_mode == "REPORT"
    assert "dunning_settings" in CompanyOut.model_fields
    assert Company.__table__.c.dunning_settings.nullable


def test_payment_promise_table():
    t = PaymentPromise.__table__
    assert {"id", "company_id", "client_id", "client_service_id", "promised_date", "source",
            "status", "reconnected", "suspension_id", "created_at", "created_by_user_id"} == set(t.c.keys())
    assert not t.c.client_service_id.nullable and t.c.suspension_id.nullable
    assert [e for e in PaymentPromiseStatus] == [PaymentPromiseStatus(v) for v in
                                                 ("OPEN", "KEPT", "BROKEN", "CANCELLED")]
    assert {e.value for e in PaymentPromiseSource} == {"PORTAL", "COLLECTOR", "BOT"}
    idx = {i.name: i for i in t.indexes}
    assert idx["uq_payment_promise_open_service"].unique
    for name in ("uq_payment_promise_open_service", "ix_payment_promise_open_date"):
        assert str(idx[name].dialect_options["postgresql"]["where"]) == "status = 'OPEN'"


def test_dunning_run_table():
    t = DunningRun.__table__
    assert {"id", "company_id", "ran_at", "mode", "outcome", "candidates", "suspended",
            "skipped"} == set(t.c.keys())
    assert {e.value for e in DunningRunOutcome} == {"OK", "TRIPPED"}


def test_rbac_pins():
    from _mi_helpers import load as ld
    rbac = ld("seeds/rbac_seed.py", "rbac_cp1_test")
    row = [p for p in rbac.PERMISSIONS_DATA if p["name"] == "captive_portal.access"]
    assert row == [_cp1().PERMISSION]
    role = isp_seed().ISP_ROLES["CAPTIVE_PORTAL"]
    assert role["permissions"] == ["captive_portal.access"]
    assert role["description"] == _cp1().ROLE["description"]
    assert Roles.CAPTIVE_PORTAL in Roles.ALL
    for other, spec in isp_seed().ISP_ROLES.items():
        if other != "CAPTIVE_PORTAL":
            assert "captive_portal.access" not in spec["permissions"]


def test_merge_settings_keeps_unpatched_keys():
    from database_utils.schemas.company import merge_settings
    stored = {"auto_suspension_mode": "ON", "promise_reconnect_enabled": True,
              "portal_contact": {"whatsapp": "1", "hours": "9-5"}}
    patch = CompanyUpdate(dunning_settings={"captive_portal_enabled": True,
                                            "portal_contact": {"whatsapp": "2"}}).dunning_settings
    out = merge_settings(stored, patch, DunningSettings)
    assert out["auto_suspension_mode"] == "ON" and out["promise_reconnect_enabled"] is True
    assert out["captive_portal_enabled"] is True
    assert out["portal_contact"]["whatsapp"] == "2" and out["portal_contact"]["hours"] == "9-5"


def test_merge_settings_validates_merged_result():
    from database_utils.schemas.company import merge_settings
    patch = CompanyUpdate(dunning_settings={"auto_suspension_mode": "OFF"}).dunning_settings
    with pytest.raises(ValidationError):
        merge_settings({"auto_suspension_mode": "ON", "promise_reconnect_enabled": True}, patch, DunningSettings)


def test_merge_settings_goals_only_keeps_working_days():
    from database_utils.schemas.company import merge_settings
    patch = CompanyUpdate(mobile_settings={"collector_daily_goal": 5}).mobile_settings
    out = merge_settings({"working_days": [0, 1]}, patch, MobileSettings)
    assert out["working_days"] == [0, 1] and out["collector_daily_goal"] == 5
