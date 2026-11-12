"""doc 70 §4: the playbooks.* overlay for a company Uplink manages (D1, D2)."""
import pytest

from _managed_helpers import admin, call, call_ok, company, custom_role_user, superadmin
from database_utils.utils.permission_utils import (
    MANAGED_PERMISSIONS, PLAYBOOKS_MANAGED_DETAIL, PermissionChecker, denied_permissions,
    playbooks_self_managed,
)

APPROVED = {"status": "APPROVED"}


def test_managed_permissions_constant():
    assert MANAGED_PERMISSIONS == {"playbooks.create", "playbooks.read", "playbooks.update", "playbooks.delete"}
    assert PLAYBOOKS_MANAGED_DETAIL == {"code": "PLAYBOOKS_MANAGED",
                                        "message": "Uplink administra los playbooks de esta empresa."}


def test_wildcard_admin_denied_on_managed_company(db):
    u = admin(db, management=None)
    assert not PermissionChecker.has_permission(u, "playbooks.read")
    assert not PermissionChecker.has_permission(u, "playbooks.*")
    assert PermissionChecker.has_permission(u, "clients.read")
    assert PermissionChecker.get_user_permissions(u) == {"*"}
    assert denied_permissions(u) == MANAGED_PERMISSIONS


@pytest.mark.parametrize("status", ["PENDING", "DENIED", "CANCELLED", "REVOKED"])
def test_not_approved_is_still_managed(db, status):
    assert denied_permissions(admin(db, management={"status": status})) == MANAGED_PERMISSIONS


def test_self_managed_company_allows_all_four(db):
    u = admin(db, management=APPROVED)
    assert all(PermissionChecker.has_permission(u, p) for p in MANAGED_PERMISSIONS)
    assert denied_permissions(u) == frozenset()


def test_custom_role_grants_are_inert(db):  # A2
    u = custom_role_user(db, {"playbooks.*", "playbooks.read", "provisioning.read"})
    assert not PermissionChecker.has_permission(u, "playbooks.update")
    assert not PermissionChecker.has_permission(u, "playbooks.read")
    assert PermissionChecker.get_user_permissions(u) == {"provisioning.read"}
    # the rows come back to life once the tenant is approved
    v = custom_role_user(db, {"playbooks.*", "provisioning.read"}, management=APPROVED)
    assert PermissionChecker.has_permission(v, "playbooks.update")
    assert PermissionChecker.get_user_permissions(v) == {"playbooks.*", "provisioning.read"}


def test_junk_management_fails_closed(db):  # Review Focus 2
    for junk in ("APPROVED", {}, {"status": "approved"}, {"reason": "x"}, ["APPROVED"], 1):
        assert playbooks_self_managed(company(db, junk)) is False
    assert playbooks_self_managed(None) is False
    assert playbooks_self_managed(company(db, APPROVED)) is True


def test_require_permission_overlay_body(db):
    e = call(db, "playbooks.read", admin(db, None))
    assert e.status_code == 403 and e.detail == PLAYBOOKS_MANAGED_DETAIL
    e = call(db, "playbooks.*", admin(db, None))
    assert e.detail == PLAYBOOKS_MANAGED_DETAIL
    # another denial keeps today's text
    e = call(db, "orders.read", custom_role_user(db, {"provisioning.read"}))
    assert e.status_code == 403 and e.detail == "Permission denied. Required permission: orders.read"
    # approved tenant passes
    u = admin(db, APPROVED)
    assert call_ok(db, "playbooks.update", u).id == u.id


def test_companyless_user_has_no_denied(db):
    assert denied_permissions(superadmin(db)) == frozenset()


def test_user_loader_eager_loads_company(db):
    from sqlalchemy import inspect
    uid = admin(db, None).id
    db.expunge_all()
    loaded = PermissionChecker.get_user_by_id_with_roles(db, uid)
    assert "company" not in inspect(loaded).unloaded
