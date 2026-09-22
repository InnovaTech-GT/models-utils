"""retire the seeded 'suspension'/'reactivation' templates AND their installed copies

Revision ID: lc2_retire_susp_react
Revises: lc1_retire_removal_tmpl

The sibling of lc1, for the same reason and by the same mechanism.

lc1 retired 'service-removal' because cancelling natively enqueues the
topology's DEPROVISION playbook. 'suspension' and 'reactivation' have the
IDENTICAL shape — trigger on a client_service status change, then
ENQUEUE_PROVISIONING with use_topology + purpose SUSPENSION/REACTIVATION — and
the lifecycle endpoint now enqueues those same playbooks directly. Leaving them
installed double-fires a device operation on every suspend and every reactivate.

Both halves of each template are now redundant, which is why retiring them
loses nothing:

  1. The billing step. Each template's first step is an UPDATE_FIELD pausing or
     resuming billing. `_apply_suspension` and `_apply_reactivation` in
     backend-erp/routers/client_services.py already do this natively
     (billing_status -> PAUSED / -> ACTIVE, and reactivation stamps
     next_generation_date when NULL so a reactivated service does not bill an
     overdue backlog). This was amendment 8's explicit intent: billing must
     never depend on an installed automation. VERIFIED against the handlers
     before writing this revision — had the native path not resumed billing,
     retiring 'reactivation' would have silently broken billing resumption.
  2. The provisioning step. Superseded by the lifecycle endpoint.

WHY NOT JUST RELY ON THE IDEMPOTENCY KEYS. The v4 templates carry
'suspend-{{trigger.resource_id}}' / 'reactivate-{{trigger.resource_id}}', so a
duplicate enqueue collapses ONLY IF the native path composes a byte-identical
key, forever. That is a correctness guarantee resting on two string literals in
different repos staying in sync — and it does not hold at all for the pre-v4
installed shape below, which has no key. Retiring is the durable fix.

TARGETING — same predicate family as lc1 (see that revision's docstring for the
full reasoning, which applies here unchanged). A workflow is deactivated iff it
is active AND both hold:
  a. it triggers on client_service / UPDATED with field_conditions on `status`
     that are either `changed_to SUSPENDED` (the suspend shape) or
     `changed_from SUSPENDED` (the reactivate shape), AND
  b. it has an ENQUEUE_PROVISIONING step whose action_config requests purpose
     SUSPENSION/REACTIVATION, or carries a 'suspend-%'/'reactivate-%'
     idempotency_key, or names NO purpose at all.

The "no purpose" arm is required, not slop — confirmed the same way lc1's was,
by reading the template's real history. The ORIGINAL installed shape (git
aaf047a, before the Cycle-3 purpose gate and before v4 added idempotency keys)
was:
    {"playbook_id": "{{param:suspend_playbook_id}}",
     "client_service_id": "{{trigger.resource_id}}", "variables": {...}}
— no `purpose`, no `idempotency_key`. Installed workflows are FROZEN COPIES
taken at install time and never converge to a later template version, so an
early adopter holds exactly that shape. Matching only on purpose/key would skip
precisely the tenants whose copies are most dangerous: with no idempotency key,
theirs double-enqueue unconditionally. An enqueue fired BY a suspension that
names no other purpose IS the suspension.

CAVEAT, same as lc1 and stated as plainly: the schema records no provenance, so
this cannot distinguish an installed copy from a hand-built tenant workflow of
the same shape. Accepted on the merits — any active workflow enqueueing a
SUSPENSION/REACTIVATION off a suspend/reactivate status change is now redundant
with the native handler and is a double-run hazard regardless of author. The
match stays narrow (both conditions required, only ENQUEUE_PROVISIONING steps
count), so a tenant workflow that merely reacts to suspension — emails the
subscriber, opens a ticket, adjusts billing — has no such step, fails condition
(b), and is untouched.

RELEASE NOTE (required): tenants who installed 'Service Suspension' or 'Service
Reactivation' will find those workflows deactivated. Suspending and reactivating
still pause/resume billing and run the topology's SUSPENSION/REACTIVATION
playbook — natively, from the lifecycle handler, gated on the playbook existing.
No tenant action is needed. A hand-built workflow matching the shape is
deactivated too and can be re-enabled from the automations UI; affected ids are
printed by this migration.

Statements only ever narrow to is_active = TRUE, so re-running is safe.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

# revision identifiers, used by Alembic.
revision: str = 'lc2_retire_susp_react'
down_revision: Union[str, Sequence[str], None] = 'lc1_retire_removal_tmpl'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Both halves are REQUIRED (AND) — see TARGETING in the module docstring.
_STALE_SUSPEND_REACTIVATE_WORKFLOWS = """
SELECT DISTINCT w.id
FROM workflow w
WHERE w.is_active = TRUE
  AND EXISTS (
      SELECT 1 FROM workflow_trigger t
      WHERE t.workflow_id = w.id
        AND t.resource_type = 'client_service'
        AND t.event_type = 'UPDATED'
        AND t.field_conditions IS NOT NULL
        AND t.field_conditions ->> 'field' = 'status'
        AND t.field_conditions ->> 'operator' IN ('changed_to', 'changed_from')
        AND upper(t.field_conditions ->> 'value') = 'SUSPENDED'
  )
  AND EXISTS (
      SELECT 1 FROM workflow_step s
      WHERE s.workflow_id = w.id
        AND s.action_type = 'ENQUEUE_PROVISIONING'
        AND (
            -- Declares SUSPENSION/REACTIVATION, or declares no purpose at all
            -- (the pre-v4 installed shape). A step explicitly naming a
            -- different purpose is left alone.
            upper(coalesce(s.action_config ->> 'purpose', ''))
                IN ('', 'SUSPENSION', 'REACTIVATION')
            OR coalesce(s.action_config ->> 'idempotency_key', '') LIKE 'suspend-%'
            OR coalesce(s.action_config ->> 'idempotency_key', '') LIKE 'reactivate-%'
        )
  )
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- Deactivate installed copies (the template rows themselves are retired
    # by the seed's convergent retirement pass, which runs after this upgrade). ---
    stale_ids = [
        row[0]
        for row in connection.execute(text(_STALE_SUSPEND_REACTIVATE_WORKFLOWS)).fetchall()
    ]
    if stale_ids:
        connection.execute(
            text("UPDATE workflow SET is_active = FALSE WHERE id = ANY(:ids)"),
            {"ids": stale_ids},
        )
    print(
        f"[lc2_retire_susp_react] deactivated {len(stale_ids)} installed workflow(s) that "
        f"enqueue a SUSPENSION/REACTIVATION off a client_service suspend/reactivate status "
        f"change — the lifecycle handler now does this natively, and a stale copy double-runs "
        f"a device operation on every suspend. Deactivated ids: {[str(i) for i in stale_ids]}"
    )


def downgrade() -> None:
    """No-op, and honestly so — same posture as lc1 and c2d_graph_removal.

    upgrade() persists nothing about WHICH workflows it deactivated beyond the
    printed migration log, and a blanket reactivation would wrongly re-enable
    workflows a tenant had deliberately turned off. Reverse by hand from the
    upgrade() log if needed.
    """
    print(
        "[lc2_retire_susp_react] downgrade: installed workflows are NOT reactivated "
        "(the ids were not persisted; blanket reactivation would re-enable workflows "
        "tenants disabled on purpose) — reverse by hand from the upgrade() log if needed"
    )
