# database_utils/dependencies/audit.py
"""
FastAPI dependencies for audit logging context.

This module provides dependencies to capture audit-related information
like user ID and IP address from requests, making it easy to add
comprehensive audit logging to any endpoint.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import Optional
from fastapi import Request

# Configure module logger
logger = logging.getLogger(__name__)


def _trusted_proxies() -> list:
    nets = []
    for raw in os.getenv("TRUSTED_PROXY_CIDRS", "").split(","):
        raw = raw.strip()
        if raw:
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                logger.warning(f"TRUSTED_PROXY_CIDRS: ignoring invalid entry {raw!r}")
    return nets


def _is_trusted(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in nets)


def get_client_ip(request: Request) -> Optional[str]:
    """
    The client IP for audit logs (security audit #99).

    `X-Forwarded-For` is trusted only when the direct peer
    (`request.client.host`) is inside env `TRUSTED_PROXY_CIDRS` (comma list;
    default empty = trust nothing), and then the RIGHT-most address that is not
    itself a trusted proxy wins: everything left of it was written by the
    client and is spoofable. `X-Real-IP` is ignored.

    Returns:
        str: IP address of the client, or None if not available
    """
    if not request.client:
        logger.warning("Could not determine client IP address")
        return None
    peer = request.client.host
    nets = _trusted_proxies()
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for and _is_trusted(peer, nets):
        for hop in reversed([h.strip() for h in forwarded_for.split(",")]):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                continue
            if not _is_trusted(hop, nets):
                return hop
    return peer

