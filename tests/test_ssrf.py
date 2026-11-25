"""Tests for the shared SSRF guard (SEC-6). Uses numeric hosts so no DNS is needed."""
import pytest

from database_utils.utils.ssrf import validate_url_no_ssrf, SSRFValidationError


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/x",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://something.internal/",
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.5.4/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "",
        "not-a-url",
    ],
)
def test_blocked(url):
    with pytest.raises(SSRFValidationError):
        validate_url_no_ssrf(url)


def test_public_ip_allowed():
    assert validate_url_no_ssrf("http://8.8.8.8/") == "8.8.8.8"


# --- 2026-11-23 audit #20/#21 -------------------------------------------------
import ipaddress  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from database_utils.utils.ssrf import is_blocked_ip, join_integration_url, pin_url  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://0.0.0.0:8000/", "http://[::]/", "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/", "http://[fe80::1]/", "http://100.100.100.200/",
    "http://[64:ff9b::a9fe:a9fe]/", "http://[fd12::1]/", "http://[::1]/",
    "http://[2002:7f00:1::]/", "http://224.0.0.1/", "http://192.0.2.1/",
    "ftp://8.8.8.8/", "file:///etc/passwd", "gopher://8.8.8.8/",
])
def test_blocked_extended(url):
    with pytest.raises(SSRFValidationError):
        validate_url_no_ssrf(url)


def test_public_ipv6_and_mapped_public_allowed():
    assert validate_url_no_ssrf("https://[2606:4700:4700::1111]/") == "2606:4700:4700::1111"
    assert not is_blocked_ip(ipaddress.ip_address("::ffff:8.8.8.8"))
    assert is_blocked_ip(ipaddress.ip_address("::ffff:10.0.0.1"))


@pytest.mark.parametrize("path", ["@evil.example/x", "//evil.example/x", "/..%2f..%2fadmin",
                                  "/a/../b", "\\evil", "/a b", "/x\r\nHost: y", "/%252e%252e/x",
                                  "/a/./b", "x"])
def test_join_refuses_rehosting_paths(path):
    with pytest.raises(SSRFValidationError):
        join_integration_url("https://api.example.com/v1", path)


def test_join_keeps_authority():
    assert join_integration_url("https://api.example.com/v1/", "/routers?id=5") == \
        "https://api.example.com/v1/routers?id=5"
    assert join_integration_url("https://api.example.com/v1", "") == "https://api.example.com/v1"


def _fake_dns(monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (ip, 0))])


def test_pin_url_replaces_host_and_keeps_sni(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34")
    p = pin_url("https://api.example.com:8443/x?y=1")
    assert p.url == "https://93.184.216.34:8443/x?y=1"
    assert p.headers == {"Host": "api.example.com:8443"}
    assert p.extensions == {"sni_hostname": "api.example.com"}


def test_pin_url_brackets_ipv6(monkeypatch):
    _fake_dns(monkeypatch, "2606:4700::1")
    p = pin_url("https://api.example.com/x")
    assert p.url == "https://[2606:4700::1]/x" and p.headers == {"Host": "api.example.com"}


def test_pin_url_refuses_rebinding_to_private(monkeypatch):
    _fake_dns(monkeypatch, "10.0.0.7")
    with pytest.raises(SSRFValidationError):
        pin_url("https://api.example.com/x")


def _integration(db, base_url="https://api.example.com"):
    from conftest import CO_A
    from database_utils.models.crm import Integration
    integ = Integration(id=uuid.uuid4(), company_id=CO_A, name="T", base_url=base_url)
    db.add(integ)
    db.commit()
    return CO_A, integ


def _fake_httpx(monkeypatch, calls):
    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, **kw):
            calls.append((method, url, kw))
            return SimpleNamespace(status_code=200, text="ok", is_success=True)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(
        Client=Client, TimeoutException=TimeoutError, RequestError=OSError))


def test_http_request_step_refuses_rehosting_path(db, monkeypatch):
    from database_utils.utils.workflow_engine import _execute_http_request
    calls = []
    _fake_httpx(monkeypatch, calls)
    co, integ = _integration(db)
    step = SimpleNamespace(action_config={"integration_id": integ.id, "method": "GET",
                                          "path": "@evil.example"})
    with pytest.raises(ValueError, match="SSRF guard"):
        _execute_http_request(db, step, {}, co)
    assert calls == []


def test_http_request_step_uses_join_and_pin(db, monkeypatch):
    from database_utils.utils.workflow_engine import _execute_http_request
    calls = []
    _fake_httpx(monkeypatch, calls)
    _fake_dns(monkeypatch, "93.184.216.34")
    co, integ = _integration(db)
    step = SimpleNamespace(action_config={"integration_id": integ.id, "method": "GET",
                                          "path": "/r", "headers": {"host": "evil", "X-A": "1"}})
    out = _execute_http_request(db, step, {}, co)
    method, url, kw = calls[0]
    assert url == "https://93.184.216.34/r"
    assert kw["headers"]["Host"] == "api.example.com" and "host" not in kw["headers"]
    assert kw["headers"]["X-A"] == "1"
    assert kw["extensions"] == {"sni_hostname": "api.example.com"}
    assert kw["follow_redirects"] is False
    assert out["url"] == "https://api.example.com/r"
