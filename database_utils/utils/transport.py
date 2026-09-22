"""Transport resolution (spec 2026-08-13 §9, canon N1/N4, doc 34 canon R23).

The ONE place that turns an InventoryItem plus its tenant's transport
configuration into the address a driver actually dials. It lives in
models-utils so the CLI drivers, the ping driver and any future frame builder
share a single implementation rather than three drifting copies.

Two invariants this module exists to hold:

  * The item is never mutated. mgmt_host/mgmt_port describe the DEVICE; this
    function describes the PATH to it (spec N1, doc 34 §1.3). If a database
    reader ever sees a gateway address in mgmt_host, something has gone wrong.

  * It fails closed. Doc 34 canon R23 rewritten: for any tenant not in
    'direct' mode, an unresolvable target is an error code, never a fall
    through to dialling the private address from wherever the worker happens
    to be running. The original R23 predicate keyed on "does this company hold
    a non-direct row", which a NAT tenant implemented as 'direct' would have
    satisfied vacuously — that is why NAT gets its own mode values.

network_access.mgmt_subnets is deliberately not read. Its documented
longest-prefix resolver was never implemented and NAT does not need it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from database_utils.models.isp import NAT_MODES, NetworkAccess


@dataclass(frozen=True)
class ResolvedEndpoint:
    host: str
    port: int
    proxy: Optional[str]   # SOCKS5 "host:port" for nat_zt, else None
    mode: str


def _default_olt_access(db, company_id) -> Optional[NetworkAccess]:
    """The tenant's default 'olt'-kind transport row. This is the same row the
    canon C19 credential resolver already needs, so callers should pass it on
    rather than querying twice."""
    return (
        db.query(NetworkAccess)
        .filter(
            NetworkAccess.company_id == company_id,
            NetworkAccess.kind == "olt",
            NetworkAccess.is_default.is_(True),
        )
        .first()
    )


def resolve_endpoint(
    db,
    item,
    company_id,
    default_port: int,
    access: Optional[NetworkAccess] = None,
) -> Tuple[Optional[ResolvedEndpoint], Optional[str]]:
    """Resolve the dial target for `item`.

    Returns (endpoint, None) or (None, error_code). Error codes are the
    provisioning failure-code vocabulary (spec N12, extended by spec
    2026-08-17 §4.2 with PYLON_NOT_PROVISIONED).

    `access` lets a caller that already loaded the default row pass it in;
    when omitted it is queried here.
    """
    if access is None:
        access = _default_olt_access(db, company_id)
    elif access.company_id != company_id:
        # Whole-branch review I4: a caller-supplied `access` row is trusted
        # verbatim — nothing here confirmed it belongs to `company_id`. In a
        # multi-tenant system, a caller that passes the wrong company's row
        # would otherwise resolve to that company's gateway: a cross-tenant
        # address leak. Today's only caller (cli.py) always queries its own
        # tenant's row, but this guard is what makes that an invariant
        # instead of a convention.
        return None, "TRANSPORT_UNAVAILABLE"

    mode = access.mode if access is not None else "direct"

    if mode in NAT_MODES:
        gateway_host = (access.gateway_host or "").strip()
        if not gateway_host:
            return None, "NAT_MAPPING_NOT_SET"
        if not item.nat_port:
            return None, "NAT_MAPPING_NOT_SET"
        proxy = None
        if mode == "nat_zt":
            proxy = (access.pylon_socks5 or "").strip() or None
            if proxy is None:
                # This tenant's Pylon was never wired, or the runbook step
                # that writes this column was skipped. Fail closed — there
                # is no fleet proxy to fall back to, by design (spec N13).
                return None, "PYLON_NOT_PROVISIONED"
        return ResolvedEndpoint(gateway_host, int(item.nat_port), proxy, mode), None

    host = (item.mgmt_host or "").strip()
    if not host:
        return None, "MGMT_HOST_NOT_SET"
    return ResolvedEndpoint(host, item.mgmt_port or default_port, None, mode), None
