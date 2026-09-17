"""retire the seeded 'service-removal' workflow template AND its installed copies

Revision ID: lc1_retire_removal_tmpl
Revises: bf1_topology_backfill

Founder decision 8: cancelling a service now natively cancels billing and
enqueues the topology's DEPROVISION playbook from the cancel handler, gated by
a pre-flight check that the playbook exists. The 'service-removal' template did
exactly that as a workflow, triggered on `client_service.status changed_to
CANCELLED` — leaving it in place double-fires the deprovision job on cancel.

TWO DISTINCT THINGS MUST BE RETIRED — this is the whole point of the revision:

1. The TEMPLATE row. Handled by the seed: 'service-removal' is deleted from
   WORKFLOW_TEMPLATES and added to RETIRED_TEMPLATE_KEYS in
   alembic/seeds/isp_seed.py; the convergent retirement pass at the end of
   _seed_workflow_templates sets workflow_template.is_active = FALSE (never
   DELETE — run history stays intact). Seeds run after upgrade via
   alembic/env.py. That pass is the ONLY reason this revision existed at
   first: migrate.yml is path-filtered on `alembic/**`, so a seed-only edit
   never reaches prod without a revision to carry it (same pattern as
   tk1_new_installation_v4).

2. The INSTALLED per-tenant WORKFLOW rows — handled HERE, in upgrade(), and
   NOT by the seed. Installing a template (backend-erp/routers/
   workflow_templates.py) materializes an INDEPENDENT `Workflow` row: there is
   no template_id/key column on `workflow` (database_utils/models/
   workflow.py), and workflow_engine.find_matching_workflows filters on
   `Workflow.is_active` alone and never joins workflow_template. Deactivating
   the template therefore has ZERO effect on tenants who already installed it.
   An earlier draft of this docstring claimed the seed pass retired "already-
   installed tenant copies" — it never did. Precedent for doing it properly:
   c2d_graph_removal step 2, whose style this follows.

Why leaving a stale copy live is not merely redundant but a correctness bug:
on a NORMAL cancel the native deprovision job is already QUEUED by the time
triggers fire, so the shared `deprovision-{client_service_id}` idempotency key
absorbs the stale workflow's duplicate enqueue. But an ADMIN FORCE-CANCEL
(force:true, used when the topology has no DEPROVISION playbook) deliberately
enqueues NOTHING and audit-logs "no DEPROVISION job enqueued". There is no
native job for the idempotency key to collide with, so the stale workflow
fires and enqueues a deprovision job against live equipment — silently
violating the exact guarantee founder decision 2 exists to make. Force-cancel
is precisely the path the idempotency key cannot protect.

TARGETING (deliberately behavioural, not provenance-based — see caveat below).
A workflow is deactivated iff it is currently active AND both hold:
  a. it has a trigger on client_service / UPDATED whose field_conditions are
     status changed_to CANCELLED, AND
  b. it has an ENQUEUE_PROVISIONING step whose action_config either requests
     purpose DEPROVISION, or carries a 'deprovision-%' idempotency_key, or
     names NO purpose at all.

The "no purpose at all" arm is not slop — it is required for correctness, and
was found by testing this predicate against the template's real history. The
ORIGINAL installed shape (git aaf047a, before the Cycle-3 E2 purpose gate and
before v4 added an idempotency key) was:
    {"playbook_id": "{{param:deprovision_playbook_id}}",
     "client_service_id": "{{trigger.resource_id}}", "variables": {...}}
— no `purpose`, no `idempotency_key`. Installed workflows are FROZEN COPIES
taken at install time; unlike the template row they never converge to a later
version. So a tenant who installed the template early holds exactly that
shape, and matching only on purpose/idempotency_key would skip them — the
worst possible miss, because with no idempotency key their stale copy
double-enqueues on a NORMAL cancel too, not just on force-cancel. An enqueue
fired BY a cancellation that names no other purpose is the deprovision.

CAVEAT, stated plainly rather than papered over: this cannot distinguish an
installed 'service-removal' copy from a hand-built tenant workflow with the
same shape, because the schema records no provenance. That is accepted here
on the merits, not shrugged off — ANY active workflow that enqueues a
DEPROVISION on `status changed_to CANCELLED` is now both (i) redundant with
the native cancel handler and (ii) the force-cancel hazard described above,
regardless of who authored it. Deactivating it is the correct outcome for a
hand-built one too. The match is kept narrow (both conditions required, and
only ENQUEUE_PROVISIONING/DEPROVISION steps count) so that a tenant workflow
which merely reacts to cancellation — emails the customer, closes a task,
opens a ticket, updates billing — is untouched: it has no DEPROVISION enqueue
and so fails condition (b).

RELEASE NOTE (required): tenants who had installed 'Service Removal' will find
that workflow deactivated after this migration. Cancelling a service still
cancels billing and runs the DEPROVISION playbook — natively, from the cancel
handler, with a pre-flight check that the playbook exists. No tenant action is
needed. Any *hand-built* workflow matching the shape above is deactivated too
and can be re-enabled from the automations UI if a tenant genuinely wants the
duplicate enqueue back; the affected workflow ids are printed by this
migration.

Prod is still pre-ISP at migrate time for this line of revisions and has no
installed copies; local/rehearsal DBs do. The statements are written to be
safely re-runnable (they only ever narrow to is_active = TRUE).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

# revision identifiers, used by Alembic.
revision: str = 'lc1_retire_removal_tmpl'
down_revision: Union[str, Sequence[str], None] = 'bf1_topology_backfill'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Both halves are REQUIRED (AND) — see TARGETING in the module docstring.
_STALE_DEPROVISION_WORKFLOWS = """
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
        AND t.field_conditions ->> 'operator' = 'changed_to'
        AND upper(t.field_conditions ->> 'value') = 'CANCELLED'
  )
  AND EXISTS (
      SELECT 1 FROM workflow_step s
      WHERE s.workflow_id = w.id
        AND s.action_type = 'ENQUEUE_PROVISIONING'
        AND (
            -- Declares DEPROVISION, or declares no purpose at all (the pre-v4
            -- installed shape: {"playbook_id": "<uuid>", ...}). An enqueue
            -- fired BY a cancellation that names no other purpose IS the
            -- deprovision. A step explicitly naming a different purpose is
            -- left alone.
            upper(coalesce(s.action_config ->> 'purpose', '')) IN ('', 'DEPROVISION')
            OR coalesce(s.action_config ->> 'idempotency_key', '') LIKE 'deprovision-%'
        )
  )
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- Deactivate installed copies (the template row itself is retired by
    # the seed's convergent retirement pass, which runs after this upgrade). ---
    stale_ids = [row[0] for row in connection.execute(text(_STALE_DEPROVISION_WORKFLOWS)).fetchall()]
    if stale_ids:
        connection.execute(
            text("UPDATE workflow SET is_active = FALSE WHERE id = ANY(:ids)"),
            {"ids": stale_ids},
        )
    print(
        f"[lc1_retire_removal_tmpl] deactivated {len(stale_ids)} installed workflow(s) that "
        f"enqueue a DEPROVISION on 'client_service.status changed_to CANCELLED' — the cancel "
        f"handler now does this natively, and a stale copy would break the ADMIN force-cancel "
        f"guarantee (no job enqueued). Deactivated ids: {[str(i) for i in stale_ids]}"
    )


def downgrade() -> None:
    """No-op, and honestly so.

    upgrade() records nothing about WHICH workflows it deactivated beyond the
    printed migration log, and a blanket reactivation would wrongly re-enable
    workflows a tenant had deliberately turned off. Same posture as
    c2d_graph_removal, which likewise does not restore the workflows it
    deactivated. To reverse by hand, re-enable the ids printed by upgrade()
    from the automations UI or with `UPDATE workflow SET is_active = TRUE WHERE
    id = ANY(...)`.
    """
    print(
        "[lc1_retire_removal_tmpl] downgrade: installed workflows are NOT reactivated "
        "(the ids were not persisted; blanket reactivation would re-enable workflows "
        "tenants disabled on purpose) — reverse by hand from the upgrade() log if needed"
    )
