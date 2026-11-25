"""The onu_web target allow-list (doc 77 D2): provisioning_settings.cpe_web_cidrs.

Pure (stdlib only), shared by the API's settings schema and the worker's
onu_web driver. The allow-list replaces the http driver's SSRF guard for ONU
web UIs on private ranges, so it fails closed: NULL/blank or unparsable text
allows nothing, a host must be a literal IP (never a name — no DNS rebinding),
and a CIDR that overlaps loopback, link-local, multicast or "this network" is
refused at save time.
"""
from __future__ import annotations

import ipaddress
from typing import List, Optional

MAX_CPE_WEB_CIDRS = 32

_FORBIDDEN = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4",
    "::/128", "::1/128", "fe80::/10", "ff00::/8",
))


def parse_cidrs(text: Optional[str]) -> List[ipaddress._BaseNetwork]:
    """Comma-separated IPv4/IPv6 CIDRs -> networks (deduplicated, in order).
    Raises ValueError on a malformed, host-bits-set or forbidden entry, or more
    than MAX_CPE_WEB_CIDRS entries. Blank -> []."""
    nets: List[ipaddress._BaseNetwork] = []
    for raw in (text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=True)
        except ValueError:
            raise ValueError(f"'{raw}' is not a network in CIDR form (e.g. 10.0.0.0/8)")
        if any(net.version == bad.version and net.overlaps(bad) for bad in _FORBIDDEN):
            raise ValueError(f"'{raw}' overlaps a loopback, link-local or multicast range")
        if net not in nets:
            nets.append(net)
    if len(nets) > MAX_CPE_WEB_CIDRS:
        raise ValueError(f"at most {MAX_CPE_WEB_CIDRS} CIDRs")
    return nets


def normalize_cidrs(text: Optional[str]) -> Optional[str]:
    """The stored form: canonical CIDRs joined by ',' — or None when blank."""
    nets = parse_cidrs(text)
    return ",".join(str(n) for n in nets) or None


def host_in_any_cidr(host: Optional[str], cidrs_text: Optional[str]) -> bool:
    """True only when `host` is a literal IP inside one of the CIDRs."""
    try:
        ip = ipaddress.ip_address((host or "").strip())
        nets = parse_cidrs(cidrs_text)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in nets)
