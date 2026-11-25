"""Single-use tokens never reach the request logs (security audit #43).

Driven through LoggingMiddleware.dispatch with a hand-built Starlette Request,
so the test needs no httpx (TestClient) in this repo's dev deps."""
import logging

import pytest
from starlette.requests import Request
from starlette.responses import Response

from database_utils.middleware.logging_middleware import LoggingMiddleware, redact_path, redact_query


@pytest.mark.parametrize("raw,expected", [
    ("/api/auth/v1/validate/abc.def", "/api/auth/v1/validate/***"),
    ("/api/auth/v1/invitations/validate/tok", "/api/auth/v1/invitations/validate/***"),
    ("/api/auth/v1/password_reset/validate/tok/extra", "/api/auth/v1/password_reset/validate/***/extra"),
    ("/api/crm/v1/clients/5", "/api/crm/v1/clients/5"),
    ("/api/auth/v1/validate/", "/api/auth/v1/validate/"),
    ("", ""),
])
def test_redact_path(raw, expected):
    assert redact_path(raw) == expected


def test_redact_query():
    assert redact_query({"Token": "t", "page": "2"}) == {"Token": "***", "page": "2"}
    assert redact_query({"code": "1", "SIGNATURE": "s", "key": "k", "password": "p", "q": "x"}) == {
        "code": "***", "SIGNATURE": "***", "key": "***", "password": "***", "q": "x"}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(getattr(record, "extra_data", {})))


@pytest.mark.parametrize("status", [200, 500])
async def test_middleware_never_logs_token(status):
    logger = logging.getLogger(f"test-redaction-{status}")
    logger.setLevel(logging.DEBUG)
    cap = _Capture()
    logger.addHandler(cap)
    mw = LoggingMiddleware(app=lambda *a: None, logger=logger)
    scope = {"type": "http", "method": "GET", "path": "/api/auth/v1/validate/SECRETTOKEN",
             "query_string": b"token=SECRETQUERY&page=2", "headers": [], "client": ("1.2.3.4", 5)}

    async def call_next(_request):
        return Response("x", status_code=status)

    await mw.dispatch(Request(scope), call_next)
    blob = "\n".join(cap.lines)
    assert cap.lines and "SECRETTOKEN" not in blob and "SECRETQUERY" not in blob
    assert "/validate/***" in blob


async def test_failed_request_never_logs_token():
    logger = logging.getLogger("test-redaction-fail")
    cap = _Capture()
    logger.addHandler(cap)
    mw = LoggingMiddleware(app=lambda *a: None, logger=logger)
    scope = {"type": "http", "method": "GET", "path": "/invitations/validate/SECRETTOKEN",
             "query_string": b"", "headers": [], "client": ("1.2.3.4", 5)}

    async def call_next(_request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await mw.dispatch(Request(scope), call_next)
    assert "SECRETTOKEN" not in "\n".join(cap.lines)
