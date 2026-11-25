"""Shared SSRF guard for outbound HTTP made on behalf of stored Integrations.

Both the interactive "test integration" endpoint (backend-erp) and the automatic
workflow HTTP_REQUEST step (workflow_engine) send requests to an operator-supplied
`base_url` with stored credentials attached. Without validation that is an SSRF
primitive (cloud metadata 169.254.169.254, localhost, internal services) plus an
exfiltration channel. This module centralises the blocklist so every outbound
path uses the same guard (SEC-6; widened by the 2026-11-23 audit, #20/#21):

- `is_blocked_ip` is the single predicate (also used by the transport resolver's
  dial-target check). It unwraps IPv4 embedded in IPv6 (mapped, 6to4, NAT64,
  Teredo) and blocks anything that is not globally routable, or multicast.
- `join_integration_url` appends a step path to a base URL without letting the
  path change the authority (`@host`, `//host`, `..`, backslashes).
- `pin_url` swaps the host for the validated IP, so the connection goes to the
  address that was checked (no second DNS lookup to rebind), while the `Host`
  header and TLS SNI keep the original name.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit


class SSRFValidationError(Exception):
    """Raised when a URL resolves to a blocked / private / internal address."""


_BLOCKED_KEYWORDS = ("localhost", "metadata", "internal")
_ALLOWED_SCHEMES = ("http", "https")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_BAD_PATH_CHARS = re.compile(r"[\\@\s\x00-\x1f\x7f]")


def _embedded_ipv4(ip) -> list:
    """IPv4 addresses an IPv6 address carries (mapped, 6to4, NAT64, Teredo)."""
    if ip.version != 6:
        return []
    out = []
    if ip.ipv4_mapped:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour:
        out.append(ip.sixtofour)
    if ip.teredo:
        out.extend(ip.teredo)
    if ip in _NAT64:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def is_blocked_ip(ip) -> bool:
    """True for any address an outbound integration call must never reach:
    not globally routable (private, loopback, link-local, CGNAT 100.64/10,
    unspecified, ULA, documentation, ...) or multicast. An IPv6 address that
    embeds an IPv4 address is judged by the embedded one too."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip)
    for inner in _embedded_ipv4(ip):
        if is_blocked_ip(inner):
            return True
    return (not ip.is_global) or ip.is_multicast


def _resolve_checked(hostname: str) -> str:
    if any(kw in hostname.lower() for kw in _BLOCKED_KEYWORDS):
        raise SSRFValidationError("Requests to internal addresses are not allowed")
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SSRFValidationError("Could not resolve hostname") from exc
    if not infos:
        raise SSRFValidationError("Could not resolve hostname")
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise SSRFValidationError("Could not resolve hostname") from exc
        if is_blocked_ip(resolved):
            raise SSRFValidationError("Requests to private network addresses are not allowed")
    return infos[0][4][0]


def validate_url_no_ssrf(url: str) -> str:
    """Validate that `url` is http(s) and its host is public; return the resolved IP.

    Rejects other schemes, hostnames containing internal keywords and any host
    that resolves to a blocked address (checking ALL A/AAAA records to cover
    round-robin DNS). Returns the first validated IP so the caller may pin to it.
    Raises SSRFValidationError on any violation.
    """
    parsed = urlsplit(url or "")
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SSRFValidationError("Only http and https URLs are allowed")
    hostname = parsed.hostname
    if not hostname:
        raise SSRFValidationError("Invalid URL: missing hostname")
    return _resolve_checked(hostname)


def join_integration_url(base_url: str, path: str) -> str:
    """`base_url` (trailing slash dropped) + `path`, refusing any path that
    could re-host the request. `path` must be empty or start with exactly one
    `/`, contain no backslash, `@`, whitespace or control character, and have
    no `.` / `..` segment even after percent-decoding. The result must keep the
    base URL's scheme and authority."""
    path = path or ""
    if path:
        if not path.startswith("/") or path.startswith("//"):
            raise SSRFValidationError("Integration path must start with a single '/'")
        if _BAD_PATH_CHARS.search(path):
            raise SSRFValidationError("Integration path contains a forbidden character")
        decoded = path.split("?", 1)[0].split("#", 1)[0]
        for _ in range(3):  # %252e -> %2e -> .
            nxt = unquote(decoded)
            if nxt == decoded:
                break
            decoded = nxt
        if _BAD_PATH_CHARS.search(decoded) or any(
            seg in (".", "..") for seg in decoded.split("/")
        ):
            raise SSRFValidationError("Integration path may not contain '.' or '..' segments")
    joined = base_url.rstrip("/") + path
    base, out = urlsplit(base_url), urlsplit(joined)
    if (out.scheme, out.netloc) != (base.scheme, base.netloc):
        raise SSRFValidationError("Integration path may not change the host")
    return joined


class PinnedRequest(NamedTuple):
    url: str
    headers: dict
    extensions: dict


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def pin_url(url: str) -> PinnedRequest:
    """Validate `url` and return it pointed at the validated IP, with the
    original `Host` header and TLS SNI name, so the request reaches exactly
    the address that was checked (DNS rebinding has no second lookup)."""
    ip = validate_url_no_ssrf(url)
    parts = urlsplit(url)
    hostname = parts.hostname
    port = f":{parts.port}" if parts.port else ""
    pinned = urlunsplit((parts.scheme, _bracket(ip) + port, parts.path, parts.query, parts.fragment))
    return PinnedRequest(
        url=pinned,
        headers={"Host": _bracket(hostname) + port},
        extensions={"sni_hostname": hostname},
    )
