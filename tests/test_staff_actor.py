"""doc 70 §5.1: a company-less superadmin acting inside one tenant (D4)."""
import uuid

import pytest

from _managed_helpers import admin, call, call_ok, company, superadmin
from database_utils.utils import permission_utils
from database_utils.utils.permission_utils import (
    ACT_COMPANY_HEADER, COMPANY_NOT_FOUND_DETAIL, MANAGED_PERMISSIONS, SELF_MANAGED_STAFF_DENIED,
    SELF_MANAGED_TENANT_DETAIL, STAFF_PERMISSIONS, PermissionChecker, StaffActor, denied_permissions,
    staff_permissions,
)

FORBIDDEN = ["clients.update", "clients.create", "client_services.update", "service_plans.update",
             "provisioning.create", "provisioning.execute", "provisioning.cancel", "device_credentials.read",
             "device_credentials.reveal", "device_credentials.update", "acs_devices.action", "orders.read",
             "playbooks.*", "users.read"]
ALLOWED = sorted(STAFF_PERMISSIONS)


def test_constants():
    assert ACT_COMPANY_HEADER == "X-Uplink-Act-Company"
    assert STAFF_PERMISSIONS == MANAGED_PERMISSIONS | {
        "provisioning.read", "device_types.read", "inventory_items.read", "service_plans.read",
        "clients.read", "client_services.read"}
    assert SELF_MANAGED_STAFF_DENIED == {"playbooks.create", "playbooks.update", "playbooks.delete"}
    assert SELF_MANAGED_TENANT_DETAIL == {
        "code": "SELF_MANAGED_TENANT",
        "message": "La empresa administra sus propios playbooks; revoque la autogestión para editarlos."}
    assert COMPANY_NOT_FOUND_DETAIL == {"code": "COMPANY_NOT_FOUND", "message": "Empresa no encontrada."}


@pytest.mark.parametrize("perm", FORBIDDEN)
def test_staff_denied_outside_set(db, perm):  # A5 (models-utils half)
    e = call(db, perm, superadmin(db), act=company(db).id)
    assert e.status_code == 403 and e.detail == f"Permission denied. Required permission: {perm}"


@pytest.mark.parametrize("perm", ALLOWED)
def test_staff_allowed_returns_scoped_actor(db, perm):
    co, sa = company(db), superadmin(db)
    actor = call_ok(db, perm, sa, act=co.id)
    assert actor.is_staff_actor and actor.company_id == co.id and actor.company_id != actor._user.company_id
    assert actor.id == sa.id and actor.company is co   # rows record the superadmin's id
    assert PermissionChecker.get_user_permissions(actor) == set(STAFF_PERMISSIONS)
    assert PermissionChecker.has_permission(actor, perm)
    assert not PermissionChecker.has_permission(actor, "clients.update")
    assert denied_permissions(actor) == frozenset()


def test_act_header_malformed_or_unknown(db):  # A13 + Review Focus 1
    for header in ("not-a-uuid", f" {uuid.uuid4()} ", "12345", "' OR 1=1 --"):
        e = call(db, "playbooks.read", superadmin(db), act=header)
        assert e.status_code == 404 and e.detail == COMPANY_NOT_FOUND_DETAIL
    assert call_ok(db, "clients.read", admin(db, None), act="").company_id   # blank header ignored
    sa = superadmin(db)
    # blank / spaces from a superadmin: acting as themselves (no roles, so the
    # generic 403, never a staff actor nor a 404)
    for header in ("", "   "):
        assert call(db, "clients.read", sa, act=header).detail == \
            "Permission denied. Required permission: clients.read"
    # surrounding spaces around a real id are tolerated
    co = company(db)
    assert call_ok(db, "playbooks.read", sa, act=f"  {co.id} ").company_id == co.id


def test_deactivated_superadmin_gets_403(db):  # A13
    e = call(db, "playbooks.read", superadmin(db, active=False), act=company(db).id)
    assert e.status_code == 403 and e.detail == "Account has been deactivated"


def test_tenant_user_header_is_ignored(db):  # A4 (models-utils half)
    a, b = admin(db, {"status": "APPROVED"}), company(db)
    got = call_ok(db, "clients.read", a, act=b.id)
    assert got.company_id == a.company_id and not getattr(got, "is_staff_actor", False)
    # a malformed header from a tenant user is ignored too, never a 404
    assert call_ok(db, "clients.read", a, act="not-a-uuid").company_id == a.company_id


def test_superadmin_with_company_is_not_staff(db):
    a = admin(db, None)
    a.is_super_admin = True
    db.commit()
    got = call_ok(db, "clients.read", a, act=company(db).id)
    assert got.company_id == a.company_id and not getattr(got, "is_staff_actor", False)


def test_self_managed_company_staff_read_only(db):  # A20 (models-utils half)
    co = company(db, {"status": "APPROVED"})
    assert call_ok(db, "playbooks.read", superadmin(db), act=co.id)
    for perm in sorted(SELF_MANAGED_STAFF_DENIED):
        e = call(db, perm, superadmin(db), act=co.id)
        assert e.status_code == 403 and e.detail == SELF_MANAGED_TENANT_DETAIL
    assert staff_permissions(co) == STAFF_PERMISSIONS - SELF_MANAGED_STAFF_DENIED
    assert staff_permissions(company(db)) == STAFF_PERMISSIONS


def test_staff_actor_wrapper_delegates_and_never_recurses(db):
    sa, co = superadmin(db), company(db)
    actor = StaffActor(sa, co)
    assert actor.email == sa.email and actor.is_super_admin and actor.company_id == co.id
    with pytest.raises(AttributeError):
        actor.no_such_attribute
    bare = StaffActor.__new__(StaffActor)   # copy/pickle path: no _user yet
    with pytest.raises(AttributeError):
        bare.anything


def test_span_attributes(db, monkeypatch):
    seen = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            seen[k] = v

    monkeypatch.setattr(permission_utils.trace, "get_current_span", lambda: Span())
    co = company(db)
    call_ok(db, "playbooks.read", superadmin(db), act=co.id)
    assert seen == {"uplink.staff_actor": True, "uplink.act_company_id": str(co.id)}
