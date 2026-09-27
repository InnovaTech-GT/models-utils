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

from dataclasses import dataclass
from typing import Optional, Tuple

from database_utils.models.isp import ProvisioningSettings


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
        return ResolvedEndpoint(gateway_host, int(item.nat_port), proxy, "gateway"), None

    if proxy_missing:
        return None, "PROXY_NOT_PROVISIONED"
    host = (item.mgmt_host or "").strip()
    if not host:
        return None, "MGMT_HOST_NOT_SET"
    return ResolvedEndpoint(host, item.mgmt_port or default_port, proxy, "device"), None
