"""require_permission refuses a deactivated user even with a valid token."""
import asyncio

import pytest
from fastapi import HTTPException

from database_utils.utils.jwt_utils import create_access_token
from database_utils.utils.permission_utils import require_permission

from test_permission_uuid import _make_admin_user, _request_with_token


def test_inactive_user_gets_403(db):
    user = _make_admin_user(db)
    token = create_access_token(user)
    user.active = False
    db.commit()
    dep = require_permission("clients.read", lambda: db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(_request_with_token(token), db=db))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Account has been deactivated"


def test_active_user_passes(db):
    user = _make_admin_user(db)
    dep = require_permission("clients.read", lambda: db)
    assert asyncio.run(dep(_request_with_token(create_access_token(user)), db=db)).id == user.id
