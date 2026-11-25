"""Transport resolution (spec 2026-08-13 §9, canon N1/N4, doc 34 canon R23,
revision `tr1_transport_axis`).

The ONE place that turns an InventoryItem plus its tenant's transport
configuration into the address a driver actually dials. It lives in
models-utils so the CLI drivers, the ping driver and any future frame builder
share a single implementation rather than three drifting copies.

The configuration is TWO orthogonal per-tenant fields on `provisioning_settings`
(canon C6's singleton), not one cross-product enum:

    host  = gateway_host if dial_target == 'gateway' else item.mgmt_host
    port  = item.nat_port if dial_target == 'gateway' else (item.mgmt_port or default_port)
    proxy = proxy_address if proxy_kind == 'socks5' else None

Three invariants this module exists to hold:

  * The item is never mutated. mgmt_host/mgmt_port describe the DEVICE; this
    function describes the PATH to it (spec N1, doc 34 §1.3). If a database
    reader ever sees a gateway address in mgmt_host, something has gone wrong.

  * It fails closed. Doc 34 canon R23: for any tenant whose path is not plain
    device-dial-no-proxy, an unresolvable target is an error code, never a fall
    through to dialling the private address from wherever the worker happens to
    be running. `proxy_kind='socks5'` with a blank/NULL `proxy_address` is a HARD
    ERROR — that is exactly why `proxy_kind` is a stored intent rather than
    `proxy_address IS NOT NULL`.

  * Absence of a `provisioning_settings` row behaves as `device` + `none`, which
    is the legitimate public-IP case and the pre-existing default. It is not a
    silent bypass of anything: a tenant with no row also has provisioning
    DISABLED (canon C6), so no job reaches a driver at all.

The hub TECHNOLOGY is not recorded and is none of this module's business. A
Railway-internal ZeroTier/Pylon proxy, an external WireGuard-hub VPS and a
future Tailscale or Nebula exit node are all `proxy_kind='socks5'` with an
address, which is why the old `PYLON_NOT_PROVISIONED`/`VPN_NOT_PROVISIONED` pair
collapsed into one `PROXY_NOT_PROVISIONED`.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

from database_utils.models.isp import ProvisioningSettings
from database_utils.utils.provisioning_codes import Codes
from database_utils.utils.ssrf import embedded_ipv4

# Hostname suffixes that only ever name the worker's own side of the network
# (`*.railway.internal` = the platform's private services, mDNS, loopback).
_WORKER_LOCAL_SUFFIXES = (".localhost", ".internal", ".local")


def _deny_cidrs() -> list:
    """PROVISIONING_DIAL_DENY_CIDRS (comma list), read per call: the
    per-deployment calibration knob (e.g. Railway's private fd00::/8)."""
    nets = []
    for raw in os.getenv("PROVISIONING_DIAL_DENY_CIDRS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning(f"PROVISIONING_DIAL_DENY_CIDRS: ignoring invalid entry {raw!r}")
    return nets


def dial_host_refusal(host: str) -> Optional[str]:
    """`Codes.DIAL_TARGET_BLOCKED` when `host` names the worker itself rather
    than a customer device (security audit #33), else None.

    Refused: IP literals (IPv4 embedded in IPv6 unwrapped) that are loopback,
    unspecified, link-local (incl. 169.254.169.254 metadata) or multicast; any
    IP inside PROVISIONING_DIAL_DENY_CIDRS; and `localhost` / `*.localhost` /
    `*.internal` / `*.local`. RFC 1918, CGNAT and public addresses are allowed —
    customer devices live there behind SOCKS or the relay. Hostnames are NOT
    resolved: over SOCKS the proxy resolves them. Non-canonical IPv4 literals
    ('127.1', '2130706433', '0x7f000001', '0177.0.0.1', '0') are parsed the
    way glibc inet_aton/getaddrinfo would and judged as the address they mean."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        name = h.rstrip(".")
        if name == "localhost" or name.endswith(_WORKER_LOCAL_SUFFIXES):
            return Codes.DIAL_TARGET_BLOCKED
        try:
            # inet_aton accepts the shorthand/decimal/hex/octal forms the
            # resolver would turn into an address; a real hostname fails here.
            ip = ipaddress.IPv4Address(socket.inet_aton(name))
        except OSError:
            return None
    deny = _deny_cidrs()
    for cand in [ip, *embedded_ipv4(ip)]:
        if (cand.is_loopback or cand.is_unspecified or cand.is_link_local
                or cand.is_multicast):
            return Codes.DIAL_TARGET_BLOCKED
        if any(cand.version == net.version and cand in net for net in deny):
            return Codes.DIAL_TARGET_BLOCKED
    return None


@dataclass(frozen=True)
class ResolvedEndpoint:
    host: str
    port: int
    proxy: Optional[str]   # SOCKS5 "host:port" when proxy_kind='socks5', else None
    # 'device' | 'gateway'. Consumers need this and NOT merely `proxy`: ICMP
    # carries no port, so a dst-nat port mapping cannot forward a ping, and
    # pinging the gateway succeeds by reaching the ROUTER — a false green that
    # promotes an uninstalled item. A gateway dial with no proxy has
    # `proxy is None`, so the ping driver cannot key that decision on the proxy.
    dial_target: str


def company_provisioning_settings(db, company_id) -> Optional[ProvisioningSettings]:
    """The tenant's provisioning/transport singleton, or None when it has never
    been created (canon C6: absence means provisioning DISABLED).

    PUBLIC on purpose, and the successor to `default_outbound_access`:
    backend-erp's cli driver and provisioning worker each carried their own
    byte-identical copy of the old `network_access` lookup, each docstring
    claiming to be the canonical one. Three copies is how a filter drifts.
    """
    return (
        db.query(ProvisioningSettings)
        .filter(ProvisioningSettings.company_id == company_id)
        .first()
    )


def resolve_endpoint(
    db,
    item,
    company_id,
    default_port: int,
    settings: Optional[ProvisioningSettings] = None,
) -> Tuple[Optional[ResolvedEndpoint], Optional[str]]:
    """Resolve the dial target for `item`.

    Returns (endpoint, None) or (None, error_code). Error codes are the
    provisioning failure-code vocabulary (spec N12):

      * NAT_MAPPING_NOT_SET   — dial_target='gateway' with a blank gateway_host,
                                or an item with no nat_port
      * MGMT_HOST_NOT_SET     — dial_target='device' with a blank item.mgmt_host
      * PROXY_NOT_PROVISIONED — proxy_kind='socks5' with a blank proxy_address
      * TRANSPORT_UNAVAILABLE — a caller-supplied `settings` row belonging to a
                                different company
      * DIAL_TARGET_BLOCKED   — the final host names the worker's own side
                                (`dial_host_refusal`; proxy_address is not checked)

    `settings` lets a caller that already loaded the row pass it in; when omitted
    it is queried here.
    """
    if settings is None:
        settings = company_provisioning_settings(db, company_id)
    elif settings.company_id != company_id:
        # Whole-branch review I4: a caller-supplied row is trusted verbatim —
        # nothing here confirmed it belongs to `company_id`. In a multi-tenant
        # system a caller that passes the wrong company's row would otherwise
        # resolve to that company's gateway or proxy: a cross-tenant address
        # leak. This guard is what makes "callers always pass their own tenant's
        # row" an invariant instead of a convention.
        return None, "TRANSPORT_UNAVAILABLE"

    dial_target = settings.dial_target if settings is not None else "device"

    # Computed up front, reported at the point the old per-mode branches
    # reported it, so the error precedence is unchanged: a gateway dial names a
    # missing mapping first, a device dial names a missing hop first.
    proxy = None
    proxy_missing = False
    if settings is not None and settings.proxy_kind == "socks5":
        # `.strip() or None` is load-bearing: ck_provisioning_settings_proxy_address
        # only demands NOT NULL, so `''` commits and must land here rather than
        # dialling unproxied.
        proxy = (settings.proxy_address or "").strip() or None
        proxy_missing = proxy is None

    if dial_target == "gateway":
        gateway_host = (settings.gateway_host or "").strip()
        if not gateway_host:
            return None, "NAT_MAPPING_NOT_SET"
        if not item.nat_port:
            return None, "NAT_MAPPING_NOT_SET"
        if proxy_missing:
            return None, "PROXY_NOT_PROVISIONED"
        refusal = dial_host_refusal(gateway_host)
        if refusal:
            return None, refusal
        return ResolvedEndpoint(gateway_host, int(item.nat_port), proxy, "gateway"), None

    if proxy_missing:
        return None, "PROXY_NOT_PROVISIONED"
    host = (item.mgmt_host or "").strip()
    if not host:
        return None, "MGMT_HOST_NOT_SET"
    refusal = dial_host_refusal(host)
    if refusal:
        return None, refusal
    return ResolvedEndpoint(host, item.mgmt_port or default_port, proxy, "device"), None


def company_dials_gateway(db, company_id) -> bool:
    """Does this tenant dial its GATEWAY (dst-nat + `nat_port`) rather than the
    device's own `mgmt_host`? (spec N10, `provisioning_settings.dial_target`.)

    The one place this is resolved, so `enqueue_connectivity_check`, the sweep
    filter and the manual test-connection endpoint can never disagree. Moved
    from backend-erp provisioning/worker.py (provisioning worker split, R1).

    ONE predicate still serves both call sites, and under the orthogonal axis it
    does so CORRECTLY rather than by coincidence — the old `mode in NAT_MODES`
    version needed a paragraph explaining why `vpn` did not belong here:
      * routers/core_devices.py — "does this tenant dial item.mgmt_host?" is
        exactly `dial_target == 'device'`, so the 422 MGMT_HOST_NOT_SET
        precondition keeps firing for a proxied device-dial tenant (the old
        `vpn`), which is what it must do.
      * here, via _connectivity_definition — "is ICMP useless on this path?"
        ICMP carries no port, so a dst-nat mapping cannot forward it: also
        exactly `dial_target == 'gateway'`. A proxied device-dial tenant keeps
        its ping step, which drivers/ping.py turns into a proxied TCP probe.
    """
    settings = company_provisioning_settings(db, company_id)
    return settings is not None and settings.dial_target == "gateway"
