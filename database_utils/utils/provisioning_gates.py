"""Provisioning blast-radius gates, enforced where every run is created
(doc 43 §5.6). Moved here from backend-erp `utils/provisioning_guards.py`,
which keeps its public names and 409 bodies and delegates to this module.

Lives in models-utils because the workflow engine's ENQUEUE_PROVISIONING
(also here) must apply them and cannot import backend-erp. `create_run`
(non-dry) refuses with ProvisioningGateError, so every run producer is gated
in ONE place: /provision, lifecycle, the cancel cascade, the ZTP closeout and
retry, and the legacy workflow engine (mode A). Mode B (one explicit job, no
run) calls `gate_failure` itself.

Two enable gates, either of which blocks a live write with PROVISIONING_DISABLED:
  1. the env kill switch `PROVISIONING_KILL_SWITCH` (default off);
  2. `device_type.provisioning_enabled` (per-device-type opt-out).
Plus the dry-run gate (canon C7): a tenant playbook goes live only when
`last_dry_run_version == version` (DRY_RUN_REQUIRED); the SaaS-verified system
playbooks are exempt. Dry runs bypass every gate (C6/C7). The tenant's
`provisioning_settings.enabled` is not a gate (dead column, Figma redesign).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database_utils.models.isp import DeviceType, Playbook

# Cycle 7 (doc 25 §5.1): per-company get-or-create connectivity probes, one per
# protocol (and NAT variant), created by backend-erp routers/core_devices.py.
CORE_CONNECTIVITY_PLAYBOOK_NAMES = frozenset({
    "core_connectivity_check_ssh",
    "core_connectivity_check_telnet",
    "core_connectivity_check_ssh-nat",
    "core_connectivity_check_telnet-nat",
})

# canon C7: seeded system playbooks are SaaS-verified and exempt from the
# per-tenant dry-run gate. Matched by name: `playbook` has no is_system flag,
# so a tenant playbook named like one is exempt too (docs/limitations.md).
SYSTEM_PLAYBOOK_NAMES = frozenset({
    "cpe_reboot",
    "cpe_factory_reset",
    "cpe_activate_tr069",
    "cpe_firmware_update",
    "huawei_onu_activate",
    "huawei_onu_plan_change",
    "huawei_onu_deactivate",
    "huawei_onu_reboot",
    "huawei_onu_deprovision",
}) | CORE_CONNECTIVITY_PLAYBOOK_NAMES


class ProvisioningGateError(Exception):
    """A live run refused by a gate. `errors` = run_gate_failures' list (each
    a 409 body plus readiness context `item_id` / `playbook_id`)."""

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        super().__init__(
            "Provisioning gates blocked: "
            + ", ".join(e.get("reason") or e["code"] for e in errors))


def kill_switch_enabled() -> bool:
    return os.getenv("PROVISIONING_KILL_SWITCH", "false").strip().lower() in ("1", "true", "yes")


def enable_gate_reason(db: Session, company_id, device_type: Optional[DeviceType]) -> Optional[str]:
    """None = the enable gates pass; else "kill_switch" | "device_type_disabled"."""
    if kill_switch_enabled():
        return "kill_switch"
    if device_type is not None and not device_type.provisioning_enabled:
        return "device_type_disabled"
    return None


def gate_failure(db: Session, company_id, playbook: Playbook,
                 device_type: Optional[DeviceType], dry_run: bool) -> Optional[Dict[str, Any]]:
    """None, or exactly backend-erp's 409 body for a live write of `playbook`
    on `device_type`."""
    if dry_run:
        return None
    reason = enable_gate_reason(db, company_id, device_type)
    if reason:
        return {"code": "PROVISIONING_DISABLED", "reason": reason}
    if playbook.name not in SYSTEM_PLAYBOOK_NAMES and playbook.last_dry_run_version != playbook.version:
        return {"code": "DRY_RUN_REQUIRED", "playbook_version": playbook.version,
                "last_dry_run_version": playbook.last_dry_run_version}
    return None


def run_gate_failures(db: Session, resolved, dry_run: bool) -> List[Dict[str, Any]]:
    """Every configured node of a resolved path is gated (Cycle 10: an OLT
    whose type is opted out blocks the run even if the CPE is allowed)."""
    if dry_run:
        return []
    failures = []
    for node in resolved.steps:
        # db.get hits the identity map for playbooks resolution already loaded.
        playbook = db.get(Playbook, node.playbook_id)
        if playbook is None:  # deleted since resolution, or a stale `resolution=`
            failure = {"code": "PLAYBOOK_NOT_FOUND"}
        else:
            device_type = db.get(DeviceType, node.device_type_id) if node.device_type_id else None
            failure = gate_failure(db, playbook.company_id, playbook, device_type, dry_run)
        if failure:
            failures.append(failure | {"item_id": str(node.item_id),
                                       "playbook_id": str(node.playbook_id)})
    return failures
