"""JWT `type` claim (mi2 auth hardening): access vs refresh, legacy tolerance."""
import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from database_utils.utils import jwt_utils
from database_utils.utils.jwt_utils import (
    create_access_token, create_refresh_token, create_token, decode_token, is_refresh_payload,
)

from test_permission_uuid import _make_admin_user, _request_with_token
from database_utils.utils.permission_utils import require_permission


USER = SimpleNamespace(id=uuid.uuid4(), roles=[], company_id=uuid.uuid4(), is_super_admin=False)


def test_access_token_claims():
    p = decode_token(create_access_token(USER))
    assert p["type"] == "access" and not is_refresh_payload(p)


def test_access_token_ttl_override():
    p = decode_token(create_access_token(USER, expires_minutes=60))
    default = decode_token(create_access_token(USER))
    assert default["exp"] - p["exp"] == (jwt_utils.access_expire - 60) * 60


@pytest.mark.parametrize("client_type,cl", [("web", "w"), ("mobile", "m")])
def test_refresh_token_claims(client_type, cl):
    p = decode_token(create_refresh_token(USER, client_type=client_type))
    assert p["type"] == "refresh" and p["cl"] == cl and is_refresh_payload(p)


def test_legacy_tokens():
    # Pre-mi2 tokens have no `type`: access ones carry roles, refresh ones don't.
    assert is_refresh_payload({"id": "x"}) is True
    assert is_refresh_payload({"id": "x", "roles": []}) is False


def test_decode_does_not_log_the_payload(caplog):
    from loguru import logger
    seen = []
    sink = logger.add(lambda m: seen.append(str(m)))
    try:
        decode_token(create_access_token(USER))
    finally:
        logger.remove(sink)
    assert not any("payload" in m for m in seen)


def test_refresh_token_is_rejected_as_bearer(db):
    user = _make_admin_user(db)
    dep = require_permission("products.read", lambda: db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(_request_with_token(create_refresh_token(user)), db=db))
    assert exc.value.status_code == 401 and exc.value.detail == "Invalid token type"


def test_legacy_refresh_token_is_rejected_as_bearer(db):
    user = _make_admin_user(db)
    legacy = create_token({"id": str(user.id)}, expires_delta=timedelta(minutes=5))
    dep = require_permission("products.read", lambda: db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(_request_with_token(legacy), db=db))
    assert exc.value.status_code == 401


def test_get_current_user_rejects_refresh_token(db):
    from database_utils.dependencies.auth import get_current_user
    user = _make_admin_user(db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(_request_with_token(create_refresh_token(user)), db=db))
    assert exc.value.status_code == 401
