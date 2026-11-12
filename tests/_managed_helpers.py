"""Fixtures shared by the doc 70 permission tests (overlay, staff actor):
tenants, users and a require_permission call with an optional act header."""
import asyncio
import uuid

from fastapi import HTTPException
from starlette.requests import Request

from database_utils.models.auth import Company, Permission, Role, Tier, User
from database_utils.utils.jwt_utils import create_access_token
from database_utils.utils.permission_utils import require_permission

ACT_HEADER = b"x-uplink-act-company"  # permission_utils.ACT_COMPANY_HEADER, lower-cased


def _tier(db):
    tier = db.query(Tier).first()
    if tier is None:
        tier = Tier(id=uuid.uuid4(), name="T", price=1, billing_cycle="MONTHLY")
        db.add(tier)
        db.flush()
    return tier


def company(db, management=None):
    co = Company(id=uuid.uuid4(), name=f"C-{uuid.uuid4()}", tier_id=_tier(db).id,
                 playbook_management=management)
    db.add(co)
    db.commit()
    return co


def _user(db, company_id, roles=(), **kw):
    u = User(id=uuid.uuid4(), name="U", email=f"{uuid.uuid4()}@e.com", age=30, password_hash="x",
             company_id=company_id, **kw)
    u.roles.extend(roles)
    db.add(u)
    db.commit()
    return u


def _global_role(db, name):
    role = db.query(Role).filter(Role.name == name, Role.company_id.is_(None)).first()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, company_id=None)
        db.add(role)
        db.flush()
    return role


def _perm(db, name):
    p = db.query(Permission).filter(Permission.name == name).first()
    if p is None:
        resource, _, action = name.partition(".")
        p = Permission(id=uuid.uuid4(), name=name, resource=resource, action=action)
        db.add(p)
        db.flush()
    return p


def admin(db, management=None):
    """A tenant user holding the built-in global ADMIN role (the `*` wildcard)."""
    co = company(db, management)
    return _user(db, co.id, [_global_role(db, "ADMIN")])


def custom_role_user(db, perms, management=None):
    co = company(db, management)
    role = Role(id=uuid.uuid4(), name="Custom", company_id=co.id)
    role.permissions.extend(_perm(db, p) for p in perms)
    return _user(db, co.id, [role])


def superadmin(db, active=True):
    return _user(db, None, is_super_admin=True, active=active)


def request_with_token(token, act=None):
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if act is not None:
        headers.append((ACT_HEADER, str(act).encode()))
    return Request({"type": "http", "headers": headers, "path": "/", "method": "GET"})


def call_ok(db, perm, user, act=None):
    dep = require_permission(perm, lambda: db)
    return asyncio.run(dep(request_with_token(create_access_token(user), act), db=db))


def call(db, perm, user, act=None):
    """Run require_permission and return the HTTPException it raised."""
    try:
        call_ok(db, perm, user, act)
    except HTTPException as e:
        return e
    raise AssertionError(f"{perm} was allowed")
