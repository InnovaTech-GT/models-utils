"""Fixed task status rules (ts1_task_status).

PENDING and ASSIGNED are not chosen by hand: they follow whether the task has
a technician. IN_PROGRESS and DONE are only ever set explicitly, and a task in
either of them keeps its status when technicians change.
"""
from typing import Optional

from database_utils.models.crm import TASK_STATUSES

ASSIGNMENT_STATUSES = ("PENDING", "ASSIGNED")
OPEN_STATUSES = ("PENDING", "ASSIGNED", "IN_PROGRESS")

# How a legacy task_state.kind maps onto the fixed status. ASSIGNED is refined
# by derive_status() once the assignment is known.
STATUS_FROM_STATE_KIND = {
    "ASSIGNED": "ASSIGNED",
    "IN_PROGRESS": "IN_PROGRESS",
    "DONE": "DONE",
    "CANCELLED": "DONE",
}


def derive_status(status: Optional[str], has_technician: bool) -> str:
    if status is not None and status not in TASK_STATUSES:
        raise ValueError(f"invalid task status {status!r}")
    if status is None or status in ASSIGNMENT_STATUSES:
        return "ASSIGNED" if has_technician else "PENDING"
    return status
