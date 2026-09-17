# database_utils/dependencies/audit.py
"""
FastAPI dependencies for audit logging context.

This module provides dependencies to capture audit-related information
like user ID and IP address from requests, making it easy to add
comprehensive audit logging to any endpoint.
"""
from __future__ import annotations

import logging
from typing import Optional
from fastapi import Request

# Configure module logger
logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> Optional[str]:
    """
    Extract the client IP address from the request.

    Checks multiple headers in order of preference:
    1. X-Forwarded-For (for proxied requests)
    2. X-Real-IP (alternative proxy header)
    3. request.client.host (direct connection)

    Args:
        request: FastAPI Request object

    Returns:
        str: IP address of the client, or None if not available
    """
    # Try X-Forwarded-For header (most common for proxied requests)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs (client, proxy1, proxy2, ...)
        # The first one is the original client
        ip = forwarded_for.split(",")[0].strip()
        logger.debug(f"IP from X-Forwarded-For: {ip}")
        return ip

    # Try X-Real-IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        logger.debug(f"IP from X-Real-IP: {real_ip}")
        return real_ip

    # Fall back to direct client IP
    if request.client:
        ip = request.client.host
        logger.debug(f"IP from request.client: {ip}")
        return ip

    logger.warning("Could not determine client IP address")
    return None

