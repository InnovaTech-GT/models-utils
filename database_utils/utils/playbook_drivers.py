"""
Context-aware playbook drivers: the single rule matrix.

Which drivers a playbook step may use depends on the TARGET device's category
tier. ssh/telnet/snmp need CORE (ssh/telnet are modelled on
inventory_item.cli_protocol, CORE-only); tr069 and connection_request need EDGE (the CPE informs to
the ACS); manual (a step the technician performs, doc 42d §5.3) is EDGE-only,
because a parked step holds its device lock for up to an hour and a shared
OLT/router must never wait for a person; ping/http/simulator are general. Fed to the bind validator
(services/playbook_bindings), the executor's runtime guard and
GET /playbooks/driver-options. Pure: DB access stays in the callers.

Moved from backend-erp `services/playbook_drivers.py` (provisioning worker
split, R1) so the worker's runtime guard reads the same matrix without
importing backend services. backend-erp keeps the 422 wrapper
(`reject_driver_mismatch`) and re-exports the rest.

The transport axis (dial_target / proxy_kind) never gates `allowed`; it only
tells the editor whether `ping` runs as ICMP or as a TCP probe (mirrors the two
downgrade guards in provisioning/drivers/ping.py).

EDGE telnet (doc 67, ONU CATV on/off) is in the pure matrix, so the worker's
runtime guard allows it, but `driver_options` / `mismatches` only treat it as
allowed when the caller passes `edge_telnet_ok` (edge_telnet.edge_telnet_ready:
identity fields, TELNET credential, tenant edge_mgmt_cidr, dial_target=device).
Without it every option and validation is unchanged. Even when ready, an EDGE
telnet step must be best effort and enable-free (edge_telnet_step_problem).
"""
from __future__ import annotations

from typing import Any, Optional

from database_utils.utils.edge_telnet import edge_telnet_step_problem

DRIVER_NOT_ALLOWED_FOR_TARGET = "DRIVER_NOT_ALLOWED_FOR_TARGET"

# Order is the UI order. snmp and connection_request are schema-valid but not
# offered in the editor (connection_request is normally the tr069 `summon` flag).
EDITOR_DRIVERS = ("ssh", "telnet", "tr069", "ping", "http", "simulator", "manual")

_GENERAL = frozenset({"ping", "simulator"})
ALLOWED_BY_TIER: dict[Optional[str], frozenset[str]] = {
    "CORE": frozenset({"ssh", "telnet", "snmp", "ping", "http", "simulator"}),
    "EDGE": frozenset({"telnet", "tr069", "connection_request", "ping", "http", "simulator", "manual"}),
    "CONSUMABLE": _GENERAL,
    "TOOL": _GENERAL,
    "OTHER": _GENERAL,
    None: _GENERAL,  # passive or unclassified plant (NULL tier)
}


def allowed_drivers(tier: Optional[str]) -> frozenset[str]:
    """A tier not in the matrix is treated as NULL."""
    return ALLOWED_BY_TIER.get(tier, ALLOWED_BY_TIER[None])


def ping_probe(dial_target: str, proxy_kind: str) -> str:
    return "tcp" if dial_target == "gateway" or proxy_kind == "socks5" else "icmp"


EDGE_TELNET_NOT_CONFIGURED = "EDGE_TELNET_NOT_CONFIGURED"


def _gated(tier: Optional[str], edge_telnet_ok: bool) -> tuple[frozenset[str], Optional[str]]:
    """(allowed set, reason for a gated-out telnet) for the editor/bind checks."""
    allowed = allowed_drivers(tier)
    if tier == "EDGE" and not edge_telnet_ok:
        return allowed - {"telnet"}, EDGE_TELNET_NOT_CONFIGURED
    return allowed, None


def driver_options(tier: Optional[str], dial_target: str, proxy_kind: str,
                   edge_telnet_ok: bool = False) -> list[dict]:
    allowed, gate = _gated(tier, edge_telnet_ok)

    def reason(key: str) -> Optional[str]:
        if key in allowed:
            return None
        return gate if gate and key == "telnet" else "TIER_MISMATCH"

    return [
        {
            "key": key,
            "allowed": key in allowed,
            "reason": reason(key),
            "probe": ping_probe(dial_target, proxy_kind) if key == "ping" else None,
        }
        for key in EDITOR_DRIVERS
    ]


def _phase_steps(definition: Any):
    """(phase_key, index, step) over every phase (doc 42 §11.1); a legacy
    `steps` shape is normalized to `configuration` first."""
    from database_utils.schemas.playbook import PHASE_KEYS, normalize_definition

    if not isinstance(definition, dict):
        definition = definition.model_dump() if hasattr(definition, "model_dump") else {}
    d = normalize_definition(definition) or {}
    for key in PHASE_KEYS.values():
        for index, step in enumerate(d.get(key) or []):
            yield key, index, step


def mismatches(definition: Any, tier: Optional[str], edge_telnet_ok: bool = False) -> list[dict]:
    allowed, gate = _gated(tier, edge_telnet_ok)
    out = []
    for phase, index, step in _phase_steps(definition):
        driver = step.get("driver") if isinstance(step, dict) else getattr(step, "driver", None)
        if driver not in allowed:
            reason = gate if gate and driver == "telnet" else None
        elif tier == "EDGE" and driver == "telnet":
            reason = edge_telnet_step_problem(definition, step)
            if reason is None:
                continue
        else:
            continue
        out.append({
            "phase": phase, "step_index": index, "driver": driver, "tier": tier,
            "allowed": sorted(allowed), "reason": reason,
        })
    return out

