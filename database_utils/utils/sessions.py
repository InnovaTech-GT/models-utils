# utils/sessions.py
"""Access-token revocation (bug-fix/access-token-revocation).

An access token issued with a refresh pair carries `sid` = the refresh token's
`family_id` (auth-erp `issue_token_pair`). The auth dependencies
(`get_current_user`, `require_permission` and everything built on them) call
`check_session`: a family with any revoked row (logout, refresh reuse, user
deactivated, password changed/reset) rejects the token with 401
`SESSION_REVOKED`.

Cost: one indexed lookup (`ix_auth_refresh_token_family_id`) per family per
SESSION_CACHE_SECONDS per process. A revoked verdict is final and cached until
the cache is cleared; a live verdict is re-checked after SESSION_CACHE_SECONDS.

Revocation latency bound: immediate in the process that revoked (it clears its
own cache — auth-erp), at most SESSION_CACHE_SECONDS (30 s) in every other
process (backend-erp replicas, other auth-erp replicas).

Legacy access tokens without `sid` (issued before this change) are accepted
until they expire — at most ACCESS_TOKEN_EXPIRE (24 h web / 60 min mobile)
after deploy — so a deploy logs nobody out.
"""
import time
import uuid
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from database_utils.utils.timezone_utils import now_gt

SESSION_CACHE_SECONDS = 30.0
_CACHE_MAX = 10_000

# sid -> (revoked, valid_until monotonic). ponytail: per-process dict, cleared
# wholesale when full; a shared cache (Redis) if cross-replica latency must be < 30 s.
_cache: dict = {}


def clear_session_cache() -> None:
    _cache.clear()


def is_session_revoked(db: Session, sid: str) -> bool:
    from database_utils.models.auth import RefreshToken

    now = time.monotonic()
    hit = _cache.get(sid)
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        family_id = uuid.UUID(str(sid))
    except ValueError:
        return True  # signed by us but malformed: never valid
    revoked = db.query(RefreshToken.jti).filter(
        RefreshToken.family_id == family_id, RefreshToken.revoked_at.isnot(None)
    ).first() is not None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[sid] = (revoked, float("inf") if revoked else now + SESSION_CACHE_SECONDS)
    return revoked


def check_session(db: Session, payload: dict) -> None:
    """401 SESSION_REVOKED when the access token's session was revoked.
    No `sid` = legacy token: accepted until its own expiry."""
    sid = payload.get("sid")
    if sid is not None and is_session_revoked(db, sid):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Session has been revoked", "code": "SESSION_REVOKED"},
        )


def revoke_user_sessions(db: Session, user_id: Optional[uuid.UUID] = None,
                         company_id: Optional[uuid.UUID] = None) -> None:
    """Revoke every refresh family of a user (or of a whole company), which
    also kills their `sid` access tokens. The caller commits."""
    from database_utils.models.auth import RefreshToken

    q = db.query(RefreshToken).filter(RefreshToken.revoked_at.is_(None))
    if user_id is not None:
        q = q.filter(RefreshToken.user_id == user_id)
    elif company_id is not None:
        q = q.filter(RefreshToken.company_id == company_id)
    else:
        raise ValueError("user_id or company_id required")
    q.update({RefreshToken.revoked_at: now_gt()}, synchronize_session=False)
    clear_session_cache()
