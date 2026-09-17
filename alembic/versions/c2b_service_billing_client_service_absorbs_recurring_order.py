"""cycle 2 entity merge R2: ClientService absorbs RecurringOrder billing

Revision ID: c2b_service_billing
Revises: c2a_catalog_merge
Create Date: 2026-07-06

Doc 18-cycle2-design.md D1 + 18a-cycle2-design-appendix.md §1b, amendments
1/3/4/8. ClientService gains the six billing columns RecurringOrder owns
today; the PG enum types `recurrenceenum`/`recurringorderstatus` are REUSED
verbatim (owned by recurring_order, created long before Cycle 2) — zero
new-enum risk. `recurring_order` is NOT mutated by this revision — engine-side
exclusion (a later backend-erp change) prevents double-generation; this is a
zero-data-loss bridge, not a table drop.

Style note: see c2a's docstring — this revision also runs in ONE transaction
(no autocommit_block), matching prod's modest scale (792 recurring_orders per
doc 17). Backfill statements are guarded (IS NULL / NOT EXISTS) so a
downgrade -> re-upgrade drill converges cleanly.

Amendments implemented here (supersede the appendix where they differ):
1. Pass 1 quantity = COALESCE(MAX(roi.quantity), 1); deterministic pick
   ORDER BY created_at, id; guard strengthened with
   NOT EXISTS(sibling already billing-migrated) so a re-run/replay can never
   double-bill two client_services for one recurring_order.
2. Pass 2 raw INSERT includes updated_at (NOT NULL, no DB default) and
   stamps activation_date/cancelled_at explicitly for CANCELLED rows (the
   @validates('status') ORM hook does not fire in raw SQL).
3. Money-continuity assertion (amendment 3): plan.price_cents ==
   product.price_cents == ROUND(product.price::numeric*100) for every
   bridged pair — abort on mismatch (a drifted price would silently change
   every subscriber's charge once backend-erp's engine cuts over to
   plan.price_cents).
4. Installed-workflow rewrite pass (amendment 8): already-INSTALLED
   (per-tenant) workflow_step rows doing UPDATE_FIELD on recurring_order.status
   via {{trigger.after.recurring_order_id}} are rewritten in place to
   client_service.billing_status via {{trigger.resource_id}} — otherwise a
   suspend/reactivate/cancel automation installed before this revision
   silently stops pausing billing (Pass A of the new engine reads
   client_service.billing_status, not recurring_order.status).
5. Downgrade reverse-materializes a recurring_order (+ single
   recurring_order_item via the service_plan.product_id bridge) for every
   client_service whose billing state would otherwise vanish on column drop
   — the exact rollback hole amendment 1 exists to close.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c2b_service_billing'
down_revision: Union[str, Sequence[str], None] = 'c2a_catalog_merge'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- v2 template definitions (verbatim, pre-c2b) — restored by downgrade() so
# a rollback never leaves a v3 (client_service.billing_status-referencing)
# blueprint installable against a schema that no longer has that column. ---
_V2_SUSPENSION_DEFINITION = {
    "parameters": [{"key": "suspend_playbook_id", "label": "Suspension playbook", "type": "playbook", "required": True}],
    "triggers": [{"resource_type": "client_service", "event_type": "UPDATED",
                  "field_conditions": {"field": "status", "operator": "changed_to", "value": "SUSPENDED"}}],
    "steps": [
        {"ref": "pause_billing", "name": "Pause recurring billing", "action_type": "UPDATE_FIELD",
         "action_config": {"resource_type": "recurring_order", "resource_id_source": "custom",
                           "resource_id": "{{trigger.after.recurring_order_id}}",
                           "updates": {"status": "PAUSED"}}},
        {"ref": "provision", "name": "Run suspend playbook", "action_type": "ENQUEUE_PROVISIONING",
         "action_config": {"playbook_id": "{{param:suspend_playbook_id}}",
                           "client_service_id": "{{trigger.resource_id}}",
                           "idempotency_key": "suspend-{{trigger.resource_id}}",
                           "variables": {"client_service_id": "{{trigger.resource_id}}"}}},
    ],
    "edges": [{"from": "pause_billing", "to": "provision"}],
}
_V2_REACTIVATION_DEFINITION = {
    "parameters": [{"key": "reactivate_playbook_id", "label": "Reactivation playbook", "type": "playbook", "required": True}],
    "triggers": [{"resource_type": "client_service", "event_type": "UPDATED",
                  "field_conditions": {"field": "status", "operator": "changed_from", "value": "SUSPENDED"}}],
    "steps": [
        {"ref": "resume_billing", "name": "Resume recurring billing", "action_type": "UPDATE_FIELD",
         "action_config": {"resource_type": "recurring_order", "resource_id_source": "custom",
                           "resource_id": "{{trigger.after.recurring_order_id}}",
                           "updates": {"status": "ACTIVE"}}},
        {"ref": "provision", "name": "Run reactivation playbook", "action_type": "ENQUEUE_PROVISIONING",
         "action_config": {"playbook_id": "{{param:reactivate_playbook_id}}",
                           "client_service_id": "{{trigger.resource_id}}",
                           "idempotency_key": "reactivate-{{trigger.resource_id}}",
                           "variables": {"client_service_id": "{{trigger.resource_id}}"}}},
    ],
    "edges": [{"from": "resume_billing", "to": "provision"}],
}
_V2_SERVICE_REMOVAL_DEFINITION = {
    "parameters": [{"key": "deprovision_playbook_id", "label": "Deprovision playbook", "type": "playbook", "required": True}],
    "triggers": [{"resource_type": "client_service", "event_type": "UPDATED",
                  "field_conditions": {"field": "status", "operator": "changed_to", "value": "CANCELLED"}}],
    "steps": [
        {"ref": "cancel_billing", "name": "Cancel recurring billing", "action_type": "UPDATE_FIELD",
         "action_config": {"resource_type": "recurring_order", "resource_id_source": "custom",
                           "resource_id": "{{trigger.after.recurring_order_id}}",
                           "updates": {"status": "CANCELLED"}}},
        {"ref": "provision", "name": "Run deprovision playbook", "action_type": "ENQUEUE_PROVISIONING",
         "action_config": {"playbook_id": "{{param:deprovision_playbook_id}}",
                           "client_service_id": "{{trigger.resource_id}}",
                           "variables": {"client_service_id": "{{trigger.resource_id}}"}}},
    ],
    "edges": [{"from": "cancel_billing", "to": "provision"}],
}
_V2_TEMPLATE_DEFINITIONS = {
    "suspension": _V2_SUSPENSION_DEFINITION,
    "reactivation": _V2_REACTIVATION_DEFINITION,
    "service-removal": _V2_SERVICE_REMOVAL_DEFINITION,
}


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- DDL: reuse the EXISTING recurrenceenum/recurringorderstatus types ---
    op.add_column('client_service', sa.Column(
        'recurrence', postgresql.ENUM(name='recurrenceenum', create_type=False), nullable=True,
    ))
    op.add_column('client_service', sa.Column('recurrence_end', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client_service', sa.Column('next_generation_date', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client_service', sa.Column('last_generated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client_service', sa.Column(
        'billing_status', postgresql.ENUM(name='recurringorderstatus', create_type=False), nullable=True,
    ))
    op.add_column('client_service', sa.Column(
        'quantity', sa.Integer(), server_default='1', nullable=False,
    ))
    op.add_column('client_service', sa.Column('migration_source', sa.String(), nullable=True))
    op.create_index(
        'ix_client_service_billing_due', 'client_service',
        ['billing_status', 'company_id', 'next_generation_date'],
    )

    # ------------------------------------------------------------------
    # Pass 1 — bridged services: client_services already pointing at a
    # recurring_order (Cycle-1 create_recurring_order=true path). Deterministic
    # oldest-first pick + a sibling guard make this safe to re-run and safe
    # against duplicate-bridge rows (two cs rows pointing at one
    # recurring_order — a known Cycle-1 possibility, doc 18 designer risk).
    # ------------------------------------------------------------------
    connection.execute(text("""
        UPDATE client_service cs SET
            recurrence = ro.recurrence,
            recurrence_end = ro.recurrence_end,
            next_generation_date = ro.next_generation_date,
            last_generated_at = ro.last_generated_at,
            billing_status = ro.status,
            quantity = COALESCE(
                (SELECT MAX(roi.quantity) FROM recurring_order_item roi WHERE roi.recurring_order_id = ro.id),
                1
            )
        FROM recurring_order ro
        WHERE cs.recurring_order_id = ro.id
          AND cs.billing_status IS NULL
          AND cs.id = (
              SELECT cs2.id FROM client_service cs2
              WHERE cs2.recurring_order_id = ro.id
              ORDER BY cs2.created_at, cs2.id
              LIMIT 1
          )
          AND NOT EXISTS (
              SELECT 1 FROM client_service sib
              WHERE sib.recurring_order_id = ro.id
                AND sib.id <> cs.id
                AND sib.billing_status IS NOT NULL
          )
    """))
    duplicate_bridges = connection.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT recurring_order_id FROM client_service
            WHERE recurring_order_id IS NOT NULL
            GROUP BY recurring_order_id HAVING COUNT(*) > 1
        ) t
    """)).scalar()
    print(f"[c2b_service_billing] duplicate-bridge recurring_orders (only oldest cs bills, "
          f"remediation is manual): {duplicate_bridges}")

    # ------------------------------------------------------------------
    # Pass 2 — unbridged recurring_orders WITH client_id AND exactly one
    # template item (prod reality is 1:1, doc 17): create a client_service.
    # Excluded (residual, served by the legacy engine indefinitely): NULL
    # client_id, zero-item, or multi-item templates.
    # ------------------------------------------------------------------
    connection.execute(text("""
        INSERT INTO client_service (
            id, created_at, updated_at, status, activation_date, cancelled_at,
            connection_params, notes, company_id, client_id, service_plan_id,
            recurring_order_id,
            recurrence, recurrence_end, next_generation_date, last_generated_at,
            billing_status, quantity, migration_source
        )
        SELECT
            gen_random_uuid(), ro.created_at, now(),
            (CASE WHEN ro.status = 'CANCELLED' THEN 'CANCELLED' ELSE 'ACTIVE' END)::clientservicestatus,
            CASE WHEN ro.status <> 'CANCELLED' THEN ro.created_at ELSE NULL END,
            CASE WHEN ro.status = 'CANCELLED'
                 THEN COALESCE(ro.recurrence_end, ro.last_generated_at, ro.created_at)
                 ELSE NULL END,
            NULL, NULL, ro.company_id, ro.client_id, sp.id,
            ro.id,
            ro.recurrence, ro.recurrence_end, ro.next_generation_date, ro.last_generated_at,
            ro.status, COALESCE(roi.quantity, 1), 'c2b'
        FROM recurring_order ro
        JOIN recurring_order_item roi ON roi.recurring_order_id = ro.id
        JOIN service_plan sp ON sp.product_id = roi.product_id
        WHERE ro.client_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM client_service cs WHERE cs.recurring_order_id = ro.id)
          AND (SELECT COUNT(*) FROM recurring_order_item x WHERE x.recurring_order_id = ro.id) = 1
    """))

    # ------------------------------------------------------------------
    # Pass 3 — extend the c1b order bridge: order.client_service_id, for
    # recurring_orders with EXACTLY ONE bridged client_service.
    # ------------------------------------------------------------------
    connection.execute(text("""
        UPDATE "order" o SET client_service_id = cs.id
        FROM client_service cs
        WHERE cs.recurring_order_id = o.recurring_order_id
          AND o.recurring_order_id IS NOT NULL
          AND o.client_service_id IS NULL
          AND (
              SELECT COUNT(*) FROM client_service cs2
              WHERE cs2.recurring_order_id = o.recurring_order_id
          ) = 1
    """))

    # --- New dedupe index: pre-assert BEFORE creating (a violating dump
    # aborts the migration with a clear message, not a cryptic index error). ---
    violator_groups = connection.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT client_service_id, due_date FROM "order"
            WHERE status <> 'CANCELLED' AND order_type = 'RECURRING' AND client_service_id IS NOT NULL
            GROUP BY 1, 2 HAVING COUNT(*) > 1
        ) t
    """)).scalar()
    if violator_groups:
        raise RuntimeError(
            f"c2b_service_billing: {violator_groups} (client_service_id, due_date) group(s) "
            f"would violate uq_order_recurring_service_due — resolve the duplicate orders "
            f"before migrating"
        )
    connection.execute(text(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_order_recurring_service_due '
        'ON "order"(client_service_id, due_date) '
        "WHERE status <> 'CANCELLED' AND order_type = 'RECURRING' AND client_service_id IS NOT NULL"
    ))

    # ------------------------------------------------------------------
    # Installed-workflow rewrite pass (amendment 8): tenant-installed copies
    # of suspension/reactivation/service-removal pause/resume/cancel billing
    # via recurring_order.status — rewrite to client_service.billing_status so
    # billing pause never depends on a stale installed automation.
    # ------------------------------------------------------------------
    rewritten = connection.execute(text("""
        UPDATE workflow_step ws
        SET action_config = json_build_object(
            'resource_type', 'client_service',
            'resource_id_source', COALESCE(ws.action_config->>'resource_id_source', 'custom'),
            'resource_id', '{{trigger.resource_id}}',
            'updates', json_build_object('billing_status', ws.action_config->'updates'->>'status')
        )
        WHERE ws.action_type = 'UPDATE_FIELD'
          AND ws.action_config->>'resource_type' = 'recurring_order'
          AND ws.action_config->>'resource_id' = '{{trigger.after.recurring_order_id}}'
          AND ws.action_config->'updates'->>'status' IN ('PAUSED', 'ACTIVE', 'CANCELLED')
        RETURNING ws.id, ws.workflow_id
    """)).fetchall()
    print(f"[c2b_service_billing] rewrote {len(rewritten)} installed workflow_step row(s) "
          f"(recurring_order.status -> client_service.billing_status)")
    for step_id, workflow_id in rewritten:
        print(f"[c2b_service_billing]   workflow={workflow_id} step={step_id}")

    # ------------------------------------------------------------------
    # End assertions — RAISE on mismatch.
    # ------------------------------------------------------------------
    failures = []

    multi_item = connection.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT recurring_order_id FROM recurring_order_item
            GROUP BY recurring_order_id HAVING COUNT(*) > 1
        ) t
    """)).scalar()
    print(f"[c2b_service_billing] recurring_orders with >1 template item (residual, left unbridged): {multi_item}")

    zero_item = connection.execute(text("""
        SELECT COUNT(*) FROM recurring_order ro
        WHERE NOT EXISTS (SELECT 1 FROM recurring_order_item roi WHERE roi.recurring_order_id = ro.id)
    """)).scalar()
    print(f"[c2b_service_billing] recurring_orders with 0 template items (residual, left unbridged): {zero_item}")

    null_client = connection.execute(text(
        "SELECT COUNT(*) FROM recurring_order WHERE client_id IS NULL"
    )).scalar()
    print(f"[c2b_service_billing] recurring_orders with NULL client_id (residual, legacy engine only): {null_client}")

    unbridged_bridgeable = connection.execute(text("""
        SELECT COUNT(*) FROM recurring_order ro
        WHERE ro.client_id IS NOT NULL
          AND (SELECT COUNT(*) FROM recurring_order_item x WHERE x.recurring_order_id = ro.id) = 1
          AND EXISTS (
              SELECT 1 FROM recurring_order_item roi
              JOIN service_plan sp ON sp.product_id = roi.product_id
              WHERE roi.recurring_order_id = ro.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM client_service cs
              WHERE cs.recurring_order_id = ro.id AND cs.billing_status IS NOT NULL
          )
    """)).scalar()
    if unbridged_bridgeable:
        failures.append(f"bridgeable recurring_orders without a billing-migrated client_service: {unbridged_bridgeable}")

    unbridged_orders = connection.execute(text("""
        SELECT COUNT(*) FROM "order" o
        JOIN (
            SELECT recurring_order_id FROM client_service
            WHERE recurring_order_id IS NOT NULL
            GROUP BY recurring_order_id HAVING COUNT(*) = 1
        ) b ON o.recurring_order_id = b.recurring_order_id
        WHERE o.client_service_id IS NULL
    """)).scalar()
    if unbridged_orders:
        failures.append(f"orders left unbridged for an unambiguous recurring_order<->client_service pair: {unbridged_orders}")

    conservation_mismatches = connection.execute(text("""
        WITH by_ro AS (
            SELECT recurring_order_id, SUM(COALESCE(total_cents, 0)) AS total
            FROM "order" WHERE recurring_order_id IS NOT NULL GROUP BY recurring_order_id
        ), by_cs AS (
            SELECT cs.recurring_order_id, SUM(COALESCE(o.total_cents, 0)) AS total
            FROM client_service cs
            JOIN "order" o ON o.client_service_id = cs.id
            WHERE cs.recurring_order_id IS NOT NULL
            GROUP BY cs.recurring_order_id
        )
        SELECT COUNT(*) FROM by_ro r JOIN by_cs c ON c.recurring_order_id = r.recurring_order_id
        WHERE r.total <> c.total
    """)).scalar()
    if conservation_mismatches:
        failures.append(f"recurring_order<->client_service order-total conservation mismatches: {conservation_mismatches}")

    # Amendment 3: money continuity.
    money_mismatches = connection.execute(text("""
        SELECT COUNT(*) FROM service_plan sp
        JOIN product p ON p.id = sp.product_id
        WHERE sp.price_cents IS DISTINCT FROM p.price_cents
           OR p.price_cents IS DISTINCT FROM ROUND(p.price::numeric * 100)::bigint
    """)).scalar()
    if money_mismatches:
        failures.append(f"plan/product price_cents continuity violations: {money_mismatches}")

    if failures:
        raise RuntimeError(
            "c2b_service_billing post-conditions FAILED — " + "; ".join(failures)
        )
    print("[c2b_service_billing] all post-condition assertions passed")


def downgrade() -> None:
    """Reverse-materialize a recurring_order for every client_service that
    would otherwise lose its billing state on column drop (amendment 1), best-
    effort reverse the installed-workflow rewrite, delete ONLY
    migration_source='c2b' rows without other dependents, restore the v2
    template definitions, then drop the added columns/index.

    NOTE (doc 18 designer risk, documented limitation): a client_service
    attached DURING the live window whose migration_source is NULL (a
    genuinely new, post-cutover service — not a c2b Pass-2 row) is still
    reverse-materialized above but NEVER deleted (only migration_source='c2b'
    rows are candidates for deletion) — this is intentional: it is real
    tenant data, not a migration artifact.
    """
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- Reverse-materialize recurring_order (+ item) for every service whose
    # billing state is not already backed by a legacy recurring_order. ---
    connection.execute(text("""
        CREATE TEMP TABLE c2b_downgrade_bridge AS
        SELECT cs.id AS cs_id, gen_random_uuid() AS new_ro_id
        FROM client_service cs
        WHERE cs.billing_status IS NOT NULL AND cs.recurring_order_id IS NULL
    """))
    bridge_count = connection.execute(text("SELECT COUNT(*) FROM c2b_downgrade_bridge")).scalar()

    connection.execute(text("""
        INSERT INTO recurring_order (
            id, created_at, recurrence, recurrence_end, last_generated_at,
            next_generation_date, status, client_id, company_id
        )
        SELECT b.new_ro_id, now(), cs.recurrence, cs.recurrence_end, cs.last_generated_at,
               cs.next_generation_date, cs.billing_status, cs.client_id, cs.company_id
        FROM c2b_downgrade_bridge b
        JOIN client_service cs ON cs.id = b.cs_id
    """))

    item_result = connection.execute(text("""
        INSERT INTO recurring_order_item (id, created_at, recurring_order_id, product_id, quantity)
        SELECT gen_random_uuid(), now(), b.new_ro_id, sp.product_id, cs.quantity
        FROM c2b_downgrade_bridge b
        JOIN client_service cs ON cs.id = b.cs_id
        JOIN service_plan sp ON sp.id = cs.service_plan_id
        WHERE sp.product_id IS NOT NULL
    """))
    itemless = bridge_count - item_result.rowcount
    if itemless:
        print(f"[c2b_service_billing] downgrade: {itemless} reverse-materialized recurring_order(s) "
              f"have NO template item (their service_plan has no product bridge — plan created "
              f"AFTER the merge; documented weakening of the rollback window, not data loss of "
              f"anything that existed pre-merge)")

    connection.execute(text("""
        UPDATE client_service cs SET recurring_order_id = b.new_ro_id
        FROM c2b_downgrade_bridge b WHERE cs.id = b.cs_id
    """))
    connection.execute(text("""
        UPDATE "order" o SET recurring_order_id = b.new_ro_id
        FROM c2b_downgrade_bridge b
        WHERE o.client_service_id = b.cs_id
          AND o.order_type = 'RECURRING'
          AND o.recurring_order_id IS NULL
    """))
    print(f"[c2b_service_billing] downgrade: reverse-materialized {bridge_count} recurring_order(s)")
    connection.execute(text("DROP TABLE c2b_downgrade_bridge"))

    # --- Best-effort reverse of the installed-workflow rewrite pass. ---
    reverted = connection.execute(text("""
        UPDATE workflow_step ws
        SET action_config = json_build_object(
            'resource_type', 'recurring_order',
            'resource_id_source', 'custom',
            'resource_id', '{{trigger.after.recurring_order_id}}',
            'updates', json_build_object('status', ws.action_config->'updates'->>'billing_status')
        )
        WHERE ws.action_type = 'UPDATE_FIELD'
          AND ws.action_config->>'resource_type' = 'client_service'
          AND ws.action_config->>'resource_id' = '{{trigger.resource_id}}'
          AND ws.action_config->'updates'->>'billing_status' IN ('PAUSED', 'ACTIVE', 'CANCELLED')
        RETURNING ws.id
    """)).fetchall()
    print(f"[c2b_service_billing] downgrade: reverted {len(reverted)} installed workflow_step row(s)")

    # --- Delete ONLY migration_source='c2b' rows without other dependents. ---
    deleted = connection.execute(text("""
        DELETE FROM client_service cs
        WHERE cs.migration_source = 'c2b'
          AND NOT EXISTS (SELECT 1 FROM service_suspension ss WHERE ss.client_service_id = cs.id)
          AND NOT EXISTS (SELECT 1 FROM inventory_item ii WHERE ii.client_service_id = cs.id)
          AND NOT EXISTS (
              SELECT 1 FROM task t WHERE t.linked_object_type = 'CLIENT_SERVICE' AND t.linked_object_id = cs.id
          )
          AND NOT EXISTS (SELECT 1 FROM provisioning_job pj WHERE pj.client_service_id = cs.id)
    """))
    print(f"[c2b_service_billing] downgrade: deleted {deleted.rowcount} migration-created "
          f"client_service row(s) without dependents")
    survivors = connection.execute(text(
        "SELECT COUNT(*) FROM client_service WHERE migration_source = 'c2b'"
    )).scalar()
    if survivors:
        print(f"[c2b_service_billing] downgrade: {survivors} migration-created client_service "
              f"row(s) kept (referenced by suspension/inventory/task/provisioning_job — "
              f"zero-data-loss)")

    # --- Restore v2 template definitions (never leave a v3 blueprint
    # referencing a dropped column installable). ---
    for key, definition in _V2_TEMPLATE_DEFINITIONS.items():
        connection.execute(
            text("UPDATE workflow_template SET definition = :definition WHERE key = :key"),
            {"definition": json.dumps(definition), "key": key},
        )
    print(f"[c2b_service_billing] downgrade: restored v2 definitions for "
          f"{list(_V2_TEMPLATE_DEFINITIONS)}")

    # --- Drop the added columns/index. ---
    connection.execute(text('DROP INDEX IF EXISTS uq_order_recurring_service_due'))
    op.drop_index('ix_client_service_billing_due', table_name='client_service')
    op.drop_column('client_service', 'migration_source')
    op.drop_column('client_service', 'quantity')
    op.drop_column('client_service', 'billing_status')
    op.drop_column('client_service', 'last_generated_at')
    op.drop_column('client_service', 'next_generation_date')
    op.drop_column('client_service', 'recurrence_end')
    op.drop_column('client_service', 'recurrence')
