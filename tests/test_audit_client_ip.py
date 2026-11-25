"""Audit client IP ignores spoofable forwarding headers (security audit #99):
X-Forwarded-For counts only from a peer inside TRUSTED_PROXY_CIDRS, and then
its right-most untrusted hop wins; X-Real-IP is never read."""
from starlette.requests import Request

from database_utils.dependencies.audit import get_client_ip


def _req(peer, headers=()):
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
                    "client": (peer, 1234) if peer else None})


def test_untrusted_peer_ignores_xff(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
    assert get_client_ip(_req("203.0.113.9", [("X-Forwarded-For", "1.2.3.4")])) == "203.0.113.9"


def test_trusted_peer_uses_rightmost_untrusted(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "100.64.0.0/10")
    req = _req("100.64.0.2", [("X-Forwarded-For", "1.2.3.4, 198.51.100.7")])
    assert get_client_ip(req) == "198.51.100.7"
    req = _req("100.64.0.2", [("X-Forwarded-For", "1.2.3.4, 198.51.100.7, 100.64.0.9")])
    assert get_client_ip(req) == "198.51.100.7"


def test_trusted_peer_without_header_or_all_trusted(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "100.64.0.0/10, junk")
    assert get_client_ip(_req("100.64.0.2")) == "100.64.0.2"
    assert get_client_ip(_req("100.64.0.2", [("X-Forwarded-For", "100.64.0.5")])) == "100.64.0.2"
    assert get_client_ip(_req("100.64.0.2", [("X-Forwarded-For", "garbage, 9.9.9.9")])) == "9.9.9.9"


def test_x_real_ip_ignored(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "100.64.0.0/10")
    assert get_client_ip(_req("203.0.113.9", [("X-Real-IP", "1.2.3.4")])) == "203.0.113.9"
    assert get_client_ip(_req("100.64.0.2", [("X-Real-IP", "1.2.3.4")])) == "100.64.0.2"


def test_no_client(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
    assert get_client_ip(_req(None, [("X-Forwarded-For", "1.2.3.4")])) is None
