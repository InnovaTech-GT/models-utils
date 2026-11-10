"""
Telnet on EDGE devices (ONU CATV on/off, doc 67): pure helpers shared by the
driver matrix, backend-erp and the provisioning worker.

The whole feature is inert unless the tenant sets
`provisioning_settings.edge_mgmt_cidr`: `edge_telnet_ready` is False without it,
so no EDGE telnet driver is offered or allowed at bind time.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Optional

# 3 x the 60 s periodic inform (doc 67 §4.1).
EDGE_FRESH_SECONDS = 180
EDGE_TELNET_PORT = 23

_HEX = re.compile(r"[^0-9A-F]")
_HEX6 = re.compile(r"[0-9A-F]{6}")


def host_in_cidr(host: Optional[str], cidr: Optional[str]) -> bool:
    """True only for an IPv4 literal inside `cidr` (no hostname, no port, no v6)."""
    if not host or not cidr:
        return False
    try:
        return ipaddress.IPv4Address(host) in ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return False


def oui_of(acs_device_id: Optional[str]) -> Optional[str]:
    """`F83451-F660-GPON51356BA9` -> `F83451`; None unless the prefix is 6 hex."""
    head = (acs_device_id or "").split("-", 1)[0].upper()
    return head if _HEX6.fullmatch(head) else None


def identity_matches(mac: Optional[str], serial: Optional[str], oui: Optional[str]) -> bool:
    """The MAC the ONU printed vs what Uplink knows (doc 67 §4.2): the last 6 hex
    equal the GPON serial's tail (acs_sync's tail rule) and, when the ACS OUI is
    known, the first 6 equal it."""
    m = _HEX.sub("", (mac or "").upper())
    tail = (serial or "")[-6:].upper()
    if len(m) != 12 or not _HEX6.fullmatch(tail) or m[-6:] != tail:
        return False
    return not oui or m[:6] == oui.upper()


def extract_identity(output: str, regex: str) -> Optional[str]:
    """The identity regex's single group in the command output, or None."""
    import re2  # lazy: only the worker runs device output through it

    match = re2.search(regex, output or "")
    return match.group(1) if match else None


def edge_telnet_ready(*, cli_platform: Optional[str], identity_command: Optional[str],
                      identity_regex: Optional[str], has_telnet_credential: bool,
                      edge_mgmt_cidr: Optional[str], dial_target: Optional[str]) -> bool:
    """Every precondition for offering/allowing telnet on an EDGE device type (§5)."""
    return bool(cli_platform and identity_command and identity_regex
                and has_telnet_credential and edge_mgmt_cidr and dial_target == "device")


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def edge_telnet_step_problem(definition: Any, step: Any) -> Optional[str]:
    """Why an EDGE telnet step is not allowed in this shape, or None.

    ONUs have no enable level and an enable secret is never sent to an
    unidentified host (EDGE_TELNET_NO_ENABLE); every EDGE telnet step is best
    effort (EDGE_TELNET_REQUIRES_WARN)."""
    session = _get(definition, "session") or {}
    if _get(session, "enable") or _get(step, "config_mode"):
        return "EDGE_TELNET_NO_ENABLE"
    if _get(step, "on_failure") != "warn":
        return "EDGE_TELNET_REQUIRES_WARN"
    return None
