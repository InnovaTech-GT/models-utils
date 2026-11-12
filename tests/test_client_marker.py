"""X-Uplink-Client marker (doc 68 §3.4): only a trimmed, case-insensitive
`mcp` marks a request; the marker never outlives its request; the active span
gets `uplink.client` only when marked. Telemetry, not security."""
import pytest
from starlette.requests import Request
from starlette.responses import Response

from database_utils.middleware.logging_middleware import LoggingMiddleware, client_marker
from database_utils.utils.logging_utils import request_client, request_client_context
from database_utils.utils.telemetry_utils import set_request_span_attributes


async def _noop_app(scope, receive, send):
    pass


def _request(headers):
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                    "query_string": b"", "client": ("127.0.0.1", 1)})


class _Span:
    def __init__(self):
        self.attrs = {}

    def is_recording(self):
        return True

    def set_attribute(self, k, v):
        self.attrs[k] = v


@pytest.mark.parametrize("raw,expected", [
    ("mcp", "mcp"), ("MCP", "mcp"), (" Mcp ", "mcp"), (None, None), ("", None),
    ("mcp2", None), ("mcp,mcp", None), ("AUTO_SUSPENSION", None), ("user", None)])
def test_client_marker_allowlist(raw, expected):
    assert client_marker(raw) == expected


async def test_marker_is_visible_in_the_request_and_cleared_after():  # Review Focus 1
    seen = []

    async def call_next(_request):
        seen.append(request_client())
        return Response("ok")

    mw = LoggingMiddleware(app=_noop_app)
    await mw.dispatch(_request({"x-uplink-client": "MCP"}), call_next)
    await mw.dispatch(_request({}), call_next)
    assert seen == ["mcp", None] and request_client() is None


def test_span_gets_uplink_client_only_when_marked():
    marked, plain = _Span(), _Span()
    token = request_client_context.set("mcp")
    set_request_span_attributes(marked)
    request_client_context.reset(token)
    set_request_span_attributes(plain)
    assert marked.attrs["uplink.client"] == "mcp" and "uplink.client" not in plain.attrs
