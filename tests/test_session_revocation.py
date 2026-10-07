"""Access-token revocation (bug-fix/access-token-revocation): `sid` claim =
refresh family; a revoked family's access tokens get 401 SESSION_REVOKED."""
import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException

from database_utils.dependencies.auth import get_current_user
from database_utils.models.auth import RefreshToken
from database_utils.utils import sessions
from database_utils.utils.jwt_utils import create_access_token, decode_token
from database_utils.utils.permission_utils import require_permission
from database_utils.utils.timezone_utils import now_gt

from test_permission_uuid import _make_admin_user, _request_with_token


@pytest.fixture(autouse=True)
def _fresh_cache():
    sessions.clear_session_cache()
    yield
    sessions.clear_session_cache()


def _family(db, user, revoked=False):
    fam = uuid.uuid4()
    db.add(RefreshToken(jti=uuid.uuid4().hex, family_id=fam, user_id=user.id, company_id=user.company_id,
                        client_type="web", expires_at=now_gt() + timedelta(days=1),
                        revoked_at=now_gt() if revoked else None))
    db.commit()
    return fam


def _deps(db):
    perm = require_permission("clients.read", lambda: db)
    return [
        lambda t: asyncio.run(get_current_user(_request_with_token(t), db=db)),
        lambda t: asyncio.run(perm(_request_with_token(t), db=db)),
    ]


def test_access_token_carries_sid():
    fam = uuid.uuid4()
    user = type("U", (), {"id": uuid.uuid4(), "roles": [], "company_id": None, "is_super_admin": False})
    assert decode_token(create_access_token(user, sid=fam))["sid"] == str(fam)
    assert "sid" not in decode_token(create_access_token(user))


@pytest.mark.parametrize("which", [0, 1])
def test_revoked_family_rejected(db, which):
    user = _make_admin_user(db)
    token = create_access_token(user, sid=_family(db, user, revoked=True))
    with pytest.raises(HTTPException) as exc:
        _deps(db)[which](token)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "SESSION_REVOKED"


@pytest.mark.parametrize("which", [0, 1])
def test_live_family_and_legacy_token_pass(db, which):
    user = _make_admin_user(db)
    dep = _deps(db)[which]
    assert dep(create_access_token(user, sid=_family(db, user))).id == user.id
    assert dep(create_access_token(user)).id == user.id  # legacy: no sid, valid until exp


def test_revoke_user_sessions_invalidates_access_tokens(db):
    user = _make_admin_user(db)
    token = create_access_token(user, sid=_family(db, user))
    dep = _deps(db)[1]
    assert dep(token).id == user.id  # now cached as live
    sessions.revoke_user_sessions(db, user_id=user.id)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        dep(token)  # revoking in this process drops the cache
    assert exc.value.detail["code"] == "SESSION_REVOKED"


def test_live_result_cached_up_to_ttl(db, monkeypatch):
    user = _make_admin_user(db)
    fam = _family(db, user)
    clock = [1000.0]
    monkeypatch.setattr(sessions.time, "monotonic", lambda: clock[0])
    assert sessions.is_session_revoked(db, str(fam)) is False
    # Revoked by another process (no local cache drop): stale for at most the TTL.
    db.query(RefreshToken).update({RefreshToken.revoked_at: now_gt()})
    db.commit()
    assert sessions.is_session_revoked(db, str(fam)) is False
    clock[0] += sessions.SESSION_CACHE_SECONDS + 0.1
    assert sessions.is_session_revoked(db, str(fam)) is True
    assert sessions.SESSION_CACHE_SECONDS <= 30


def test_malformed_sid_rejected(db):
    assert sessions.is_session_revoked(db, "not-a-uuid") is True
