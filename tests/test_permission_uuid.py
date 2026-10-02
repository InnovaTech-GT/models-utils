"""Regression test for permission_dependency UUID coercion.

The JWT carries the user id as a string. `User.id` is a SQLAlchemy `Uuid`
column whose bind processor requires a `uuid.UUID` object. PostgreSQL tolerates
the string via the driver, but SQLite raises
``AttributeError: 'str' object has no attribute 'hex'``. `permission_dependency`
must coerce the string to `uuid.UUID` before querying so both dialects work.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from database_utils.database import Base
from database_utils.models.auth import Company, Role, Tier, User
from database_utils.utils.jwt_utils import create_access_token
from database_utils.utils.permission_utils import require_permission


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _make_admin_user(db):
    tier = Tier(id=uuid.uuid4(), name="T", price=1, billing_cycle="MONTHLY")
    db.add(tier)
    db.commit()
    company = Company(id=uuid.uuid4(), name="C", email="c@e.com", tier_id=tier.id)
    db.add(company)
    db.commit()
    user = User(
        id=uuid.uuid4(),
        name="U",
        email="u@e.com",
        age=30,
        password_hash="x",
        company_id=company.id,
        is_super_admin=True,
    )
    # The built-in ADMIN role is global; a tenant role named ADMIN gets no wildcard.
    role = Role(id=uuid.uuid4(), name="ADMIN", company_id=None)
    user.roles.append(role)
    db.add_all([user, role])
    db.commit()
    return user


def _request_with_token(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "path": "/",
        "method": "GET",
    }
    return Request(scope)


def test_permission_dependency_coerces_string_id(db):
    """A string-id JWT resolves to the user under SQLite (no 'hex' crash)."""
    user = _make_admin_user(db)
    token = create_access_token(user)  # payload id is a string

    dependency = require_permission("products.read", lambda: db)
    resolved = asyncio.run(dependency(_request_with_token(token), db=db))

    assert resolved.id == user.id
