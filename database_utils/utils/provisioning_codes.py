"""Typed provisioning failure codes (spec N12, doc 34 canon R13) — the
shared vocabulary.

Moved from backend-erp `provisioning/errors.py` (provisioning worker split,
R1): the worker produces these codes and the API classifies them (run events,
office-only codes), so the vocabulary lives here and neither side imports the
other. The driver-side half (stages, `RETRYABLE`, `is_transient`,
`classify_connect_error`) stays with the worker, which re-exports these names
from `provisioning/errors.py`.
"""
from __future__ import annotations


class Codes:
    NAT_MAPPING_NOT_SET = "NAT_MAPPING_NOT_SET"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    # `proxy_kind='socks5'` with a blank/NULL `proxy_address`
    # (tr1_transport_axis). Replaces BOTH PYLON_NOT_PROVISIONED and
    # VPN_NOT_PROVISIONED: the hub TECHNOLOGY is not recorded any more and was
    # never the resolver's business — a Railway-internal ZeroTier/Pylon proxy,
    # an external WireGuard-hub VPS and a future Tailscale exit node are one
    # code, not three.
    PROXY_NOT_PROVISIONED = "PROXY_NOT_PROVISIONED"
    MGMT_HOST_NOT_SET = "MGMT_HOST_NOT_SET"
    NO_ROUTE = "NO_ROUTE"
    CONNECT_REFUSED = "CONNECT_REFUSED"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    HOST_KEY_MISMATCH = "HOST_KEY_MISMATCH"
    # Reserved, not yet producible: spec N9's telnet-banner-assertion (no SSH
    # host-key concept over telnet) is out of scope this cycle.
    DEVICE_IDENTITY_MISMATCH = "DEVICE_IDENTITY_MISMATCH"
    # netmiko's ReadTimeout ("Pattern not detected: ... in output.") fires
    # AFTER the TCP connect and the SSH/telnet auth both already succeeded —
    # the session opened, but the device's prompt never matched the expected
    # regex (e.g. RouterOS's colour-coded interactive prompt without `+ct`).
    # Reporting that as CONNECT_TIMEOUT is actively misleading: it points
    # investigation at the transport/proxy, which was never the problem.
    PROMPT_NOT_MATCHED = "PROMPT_NOT_MATCHED"
    CONNECT_FAILED = "CONNECT_FAILED"
    # --- engine v2 (doc 42 §8.2.1, §9.3) ---
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    # session.enable failed: no CLI_ENABLE bound, wrong secret, or the
    # privileged prompt never appeared. Deterministic, stage connect.
    ENABLE_FAILED = "ENABLE_FAILED"
    # The device refused a command (session.error_patterns, http 4xx, NBI 4xx).
    COMMAND_REJECTED = "COMMAND_REJECTED"
    # netmiko ReadTimeout AFTER login, during a command (was PROMPT_NOT_MATCHED).
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    DEVICE_BUSY = "DEVICE_BUSY"
    ENDPOINT_UNAVAILABLE = "ENDPOINT_UNAVAILABLE"
    # Any failure the executor produces before a driver call (unresolved
    # template, missing variable, guard breach, SSRF refusal). Stage render.
    RENDER_FAILED = "RENDER_FAILED"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    # A settled tr069 read whose value the ACS does not hold yet (doc 42 §9.6).
    CPE_NOT_CONNECTED = "CPE_NOT_CONNECTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CAPTURE_NOT_FOUND = "CAPTURE_NOT_FOUND"
    CAPTURE_UNSAFE = "CAPTURE_UNSAFE"
    THRESHOLD_VIOLATED = "THRESHOLD_VIOLATED"
    REGEX_UNSUPPORTED = "REGEX_UNSUPPORTED"


# Standalone and legacy jobs (doc 42 §8.2.7): today's restart-from-step-0
# rule refuses these. Moved from worker.py (and later from backend-erp
# provisioning/errors.py) so run-event classification can share it
# without importing the worker. The validation/capture codes stay out: they
# are field-fixable, and doc 43 reads this set as "office only".
NON_RETRYABLE_CODES = frozenset({
    Codes.HOST_KEY_MISMATCH,
    Codes.NAT_MAPPING_NOT_SET,
    Codes.TRANSPORT_UNAVAILABLE,
    Codes.MGMT_HOST_NOT_SET,
    Codes.PROXY_NOT_PROVISIONED,
    Codes.PROMPT_NOT_MATCHED,
    Codes.ENABLE_FAILED,
    Codes.COMMAND_REJECTED,
    Codes.RENDER_FAILED,
})

