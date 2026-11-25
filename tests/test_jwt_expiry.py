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


@pytest.mark.parametrize("env", [" Production ", "PRODUCTION"])
def test_missing_secret_raises_in_production_any_spelling(monkeypatch, env):
    with pytest.raises(RuntimeError):
        _reload_jwt(monkeypatch, ENVIRONMENT=env)


def test_missing_secret_refused_when_environment_unset(monkeypatch):
    # Security audit #23: no well-known signing key outside ENVIRONMENT=test.
    m = _reload_jwt(monkeypatch)  # import still succeeds (worker/cron never sign)
    assert not hasattr(m, "secret_key")
    with pytest.raises(RuntimeError, match="SECRET_KEY must be set"):
        m.create_token({"id": "u"})
    with pytest.raises(RuntimeError, match="SECRET_KEY must be set"):
        m.decode_token("a.b.c")


@pytest.mark.parametrize("env", ["development", "staging", "prod", "local"])
def test_missing_secret_refused_outside_test(monkeypatch, env):
    m = _reload_jwt(monkeypatch, ENVIRONMENT=env)
    with pytest.raises(RuntimeError, match="SECRET_KEY must be set"):
        m.get_secret_key()


def test_missing_secret_falls_back_only_in_test(monkeypatch):
    m = _reload_jwt(monkeypatch, ENVIRONMENT="test")
    assert m.decode_token(m.create_token({"id": "u"}))["id"] == "u"


def test_secret_key_read_per_call(monkeypatch):
    m = _reload_jwt(monkeypatch, SECRET_KEY="a" * 32)
    token = m.create_token({"id": "u"})
    monkeypatch.setenv("SECRET_KEY", "b" * 32)
    with pytest.raises(Exception):
        m.decode_token(token)
    assert m.get_secret_key() == "b" * 32


@pytest.mark.parametrize("env,expected", [("production", True), (" Production ", True),
                                          ("development", False), ("", False), ("test", False)])
def test_is_production(monkeypatch, env, expected):
    m = _reload_jwt(monkeypatch, SECRET_KEY="x" * 32)
    monkeypatch.setenv("ENVIRONMENT", env)
    assert m.is_production() is expected


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    # Ensure a valid module state for any later imports in the session.
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    import database_utils.utils.jwt_utils as jwt_utils
    importlib.reload(jwt_utils)
