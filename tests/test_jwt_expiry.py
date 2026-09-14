"""Regression tests for JWT expiry/secret configuration (SEC-1, SEC-2).

The access-token TTL must be read from ACCESS_TOKEN_EXPIRE (minutes), not the
old ACCESS_TOKEN_EXPIRE_MINUTES that no environment ever set (which left the
114400-minute ~79-day default silently in effect).
"""
import importlib

import pytest


def _reload_jwt(monkeypatch, **env):
    for k in ("ACCESS_TOKEN_EXPIRE", "ACCESS_TOKEN_EXPIRE_MINUTES", "SECRET_KEY", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import database_utils.utils.jwt_utils as jwt_utils
    return importlib.reload(jwt_utils)


def test_access_expire_reads_access_token_expire(monkeypatch):
    m = _reload_jwt(monkeypatch, ACCESS_TOKEN_EXPIRE="1440", SECRET_KEY="x" * 32)
    assert m.access_expire == 1440


def test_access_expire_defaults_to_one_day_not_79(monkeypatch):
    # No ACCESS_TOKEN_EXPIRE set -> must default to 1440 (1 day), never 114400.
    m = _reload_jwt(monkeypatch, SECRET_KEY="x" * 32)
    assert m.access_expire == 1440


def test_missing_secret_raises_in_production(monkeypatch):
    with pytest.raises(RuntimeError):
        _reload_jwt(monkeypatch, ENVIRONMENT="production")


def test_missing_secret_falls_back_outside_production(monkeypatch):
    m = _reload_jwt(monkeypatch)  # no ENVIRONMENT, no SECRET_KEY
    assert m.secret_key == "default-secret-key-for-development"


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    # Ensure a valid module state for any later imports in the session.
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    import database_utils.utils.jwt_utils as jwt_utils
    importlib.reload(jwt_utils)
