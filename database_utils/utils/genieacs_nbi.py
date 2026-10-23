"""
Read-side GenieACS NBI helpers (Cycle 5 Phase 1, canon C3/C4), shared by the
worker's tr069 driver and poller and by the API's ACS administration (device
reads, the refreshObject read task, tag removal, inform-credential rollout).

Moved verbatim from backend-erp `provisioning/drivers/genieacs.py`
(provisioning worker split, R1), which re-exports them, so the API reaches the
NBI without importing the provisioning package (and its netmiko drivers).

Env: `GENIEACS_NBI_URL`, optional basic auth `GENIEACS_NBI_USER` /
`GENIEACS_NBI_PASSWORD` (the prod caddy sidecar), `GENIEACS_NBI_TIMEOUT_SECONDS`.
`httpx` is imported inside `nbi_client`: models-utils does not depend on it.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    import httpx

GENIEACS_NBI_TIMEOUT_SECONDS = float(os.getenv("GENIEACS_NBI_TIMEOUT_SECONDS", "10"))

# GenieACS core fields that carry the device serial across data models
# (TR-098 vs TR-181, doc 23 §3 NBI facts). `_deviceId._SerialNumber` is
# GenieACS-internal and always populated regardless of data model; the other
# two are the common TR-098/TR-181 parameter paths, kept as a fallback for
# devices informed before GenieACS indexed `_deviceId`.
_SERIAL_PATHS = (
    "_deviceId._SerialNumber",
    "InternetGatewayDevice.DeviceInfo.SerialNumber",
    "Device.DeviceInfo.SerialNumber",
)


def tenant_tag(company_id) -> str:
    """canon C4: dash, not colon — a colon breaks GenieACS parameter-path/
    query segments. Single source of truth reused by worker.py's poller and
    the acs router."""
    return f"t-{company_id}"


def nbi_base_url() -> str:
    url = os.getenv("GENIEACS_NBI_URL")
    if not url:
        raise RuntimeError("GENIEACS_NBI_URL is not set")
    return url.rstrip("/")


def _nbi_auth() -> Optional[Tuple[str, str]]:
    user = os.getenv("GENIEACS_NBI_USER")
    password = os.getenv("GENIEACS_NBI_PASSWORD")
    if user:
        return (user, password or "")
    return None


def nbi_client() -> httpx.Client:
    """A fresh httpx.Client against the NBI. Callers are responsible for
    closing it (use as a context manager) — drivers run under
    `asyncio.to_thread`, so a plain sync client is correct here."""
    import httpx  # lazy: models-utils does not install httpx; both consumers do

    return httpx.Client(
        base_url=nbi_base_url(),
        auth=_nbi_auth(),
        timeout=GENIEACS_NBI_TIMEOUT_SECONDS,
    )


def find_device_id(client: httpx.Client, company_id, serial: str) -> Optional[str]:
    """Resolve the GenieACS device `_id` for `serial`, scoped to the tenant
    tag. Never trusts a caller-supplied device id — this is the tenant
    chokepoint (canon C4): a device outside the tag never matches."""
    tag = tenant_tag(company_id)
    serial_or = {"$or": [{path: serial} for path in _SERIAL_PATHS]}
    query = {"$and": [{"_tags": tag}, serial_or]}
    resp = client.get("/devices/", params={"query": json.dumps(query), "projection": "_id"})
    resp.raise_for_status()
    devices = resp.json()
    if not devices:
        return None
    return devices[0].get("_id")
