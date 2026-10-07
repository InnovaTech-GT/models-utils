"""rr1_four_builtin_roles: only ADMIN / VIEWER / COLLECTOR / TECHNICIAN are
built in, and only the GLOBAL ADMIN role carries the wildcard.

Seeds/revisions are loaded by file path (test_attested_adoption precedent).
"""
import asyncio
import importlib.util
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from database_utils.constants.roles import Roles
from database_utils.dependencies.auth import get_admin_user, require_roles
from database_utils.utils.permission_utils import PermissionChecker

_HERE = os.path.dirname(__file__)
_ALEMBIC = os.path.join(_HERE, "..", "alembic")


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ALEMBIC, rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _role(name, company_id=None, permissions=()):
    perms = [SimpleNamespace(name=p) for p in permissions]
    return SimpleNamespace(name=name, company_id=company_id, permissions=perms)


def _user(*roles):
    return SimpleNamespace(id="u", name="u", roles=list(roles))


def test_constants_are_the_four_builtin_roles():
    assert Roles.ALL == {"ADMIN", "VIEWER", "COLLECTOR", "TECHNICIAN"}


def test_revision_remaps_into_builtin_roles_only():
    rr1 = _load("versions/rr1_four_builtin_roles.py", "rr1")
    assert rr1.down_revision == "ci1_category_icons"
    assert set(rr1.BUILTIN_ROLES) == Roles.ALL
    assert set(rr1.ROLE_REMAP.values()) <= Roles.ALL
    assert set(rr1.ROLE_REMAP).isdisjoint(Roles.ALL)
    assert rr1.ROLE_REMAP["MANAGER"] == "ADMIN"
    assert rr1.ROLE_REMAP["BILLING"] == "COLLECTOR"


def test_web_access_row_matches_the_seed():
    rr1 = _load("versions/rr1_four_builtin_roles.py", "rr1")
    rbac = _load("seeds/rbac_seed.py", "rbac_seed_rr1_test")
    rows = [p for p in rbac.PERMISSIONS_DATA if p["name"] == "web.access"]
    assert rows == [rr1.WEB_ACCESS]


def test_isp_seed_only_creates_the_mobile_roles():
    isp = _load("seeds/isp_seed.py", "isp_seed_rr1_test")
    assert set(isp.ISP_ROLES) == {"COLLECTOR", "TECHNICIAN"}
    for spec in isp.ISP_ROLES.values():
        assert "web.access" not in spec["permissions"]


def test_rbac_seed_creates_no_removed_role():
    source = open(os.path.join(_ALEMBIC, "seeds", "rbac_seed.py"), encoding="utf-8").read()
    for name in ("MANAGER", "SALES", "USER"):
        assert f'"name": "{name}"' not in source


def test_global_admin_gets_wildcard():
    assert PermissionChecker.get_user_permissions(_user(_role("ADMIN"))) == {"*"}


def test_custom_role_named_admin_gets_no_wildcard():
    user = _user(_role("ADMIN", company_id="tenant", permissions=["clients.read"]))
    assert PermissionChecker.get_user_permissions(user) == {"clients.read"}
    assert not PermissionChecker.has_permission(user, "roles.delete")


def test_get_admin_user_ignores_custom_admin_name():
    assert asyncio.run(get_admin_user(_user(_role("ADMIN")))).id == "u"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_admin_user(_user(_role("ADMIN", company_id="tenant"))))
    assert exc.value.status_code == 403


def test_require_roles_ignores_custom_role_names():
    checker = require_roles([Roles.ADMIN])
    assert asyncio.run(checker(_user(_role("ADMIN")))).id == "u"
    with pytest.raises(HTTPException):
        asyncio.run(checker(_user(_role("ADMIN", company_id="tenant"))))
