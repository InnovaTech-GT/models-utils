"""doc 70 §8: the only writer of `playbook_revision`, one row per playbook
write. Called inside the caller's transaction, before the commit that persists
the change: it adds and flushes, never commits, so a playbook write never
commits without its revision (and a failing revision insert rolls it back).

Pure like the rest of the shared provisioning code (the worker's connectivity
sweep reaches it): no fastapi or backend imports."""
from __future__ import annotations

from typing import Optional

from database_utils.models.isp import PLAYBOOK_REVISION_ACTIONS, PlaybookRevision

_NO_DEFINITION = ("DELETE", "UNBIND")


def record_revision(db, actor, playbook, action: str, target: Optional[dict] = None) -> PlaybookRevision:
    """`actor` is the ORM user, a `StaffActor` (its `id` is the superadmin's)
    or None for system writes. `definition` is the playbook's definition
    after the change (None for DELETE / UNBIND); `target` is
    {scope, target_id, purpose} for BIND / UNBIND."""
    if action not in PLAYBOOK_REVISION_ACTIONS:
        raise ValueError(f"unknown playbook revision action: {action!r}")
    db.flush()  # a just-added playbook gets its id and default version
    rev = PlaybookRevision(
        company_id=playbook.company_id,
        playbook_id=playbook.id,
        playbook_name=playbook.name,
        version=playbook.version,
        action=action,
        definition=None if action in _NO_DEFINITION else playbook.definition,
        target=target,
        actor_user_id=getattr(actor, "id", None),
        actor_staff=getattr(actor, "is_staff_actor", False),
    )
    db.add(rev)
    db.flush()
    return rev
