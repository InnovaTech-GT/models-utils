"""legacy drop: product / recurring_order / task_state / workflow_template

Revision ID: ld1_legacy_drop
Revises: rr1_four_builtin_roles
Create Date: 2026-10-02

DESTRUCTIVE. Ships with models-utils 4.0.0. Every consumer (backend-erp,
auth-erp, frontend-erp, uplink-mcp) must already run code that no longer
touches these tables BEFORE this revision reaches a database.

Order, and no other:

  a. grant-copy    products.* -> service_plans.*, recurring_orders.* ->
                   client_services.* (the pairs rbac_seed step 4 used to
                   converge), once, before the source permissions are deleted
  b. migrate       every ACTIVE recurring_order that no client_service bills
                   (the legacy "Pass B"-only subscriptions) becomes a
                   client_service, built exactly like c2b Pass 2; its orders
                   are repointed via order.client_service_id. A row that cannot
                   be migrated safely RAISES — nothing is silently dropped
  c. workflows     delete tenant workflows that trigger on / write
                   recurring_order, product or task_state (FKs cascade to
                   triggers, steps, edges, executions)
  d. permissions   delete products.% / recurring_orders.% / task_states.% /
                   workflow_templates.% (role_permission cascades)
  e. columns       order.recurring_order_id, order_item.product_id,
                   service_plan.product_id, client_service.recurring_order_id,
                   task.task_state_id (+ their indexes)
  f. tables        recurring_order_item, recurring_order, product, task_state,
                   workflow_template
  g. asserts

The PG enums recurrenceenum / recurringorderstatus stay (client_service uses
them). The RECURRING_ORDER label of tasklinkedobjecttype also stays (Postgres
cannot drop an enum label); rows using it are nulled here.

Idempotent: every step is guarded by table/column existence, so a re-run on a
migrated database is a no-op.

Not reversible. downgrade() raises: the dropped catalog, billing templates and
task columns cannot be reconstructed, and the unbridged subscriptions were
folded into client_service. Restore from a backup taken before the release.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ld1_legacy_drop"
down_revision: Union[str, None] = "rr1_four_builtin_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TAG = "[ld1_legacy_drop]"

# Same pairs rbac_seed step 4 carried (suspend/reactivate/generate included).
GRANT_COPY = (
    ("products.create", "service_plans.create"),
    ("products.read", "service_plans.read"),
    ("products.update", "service_plans.update"),
    ("products.delete", "service_plans.delete"),
    ("recurring_orders.create", "client_services.create"),
    ("recurring_orders.read", "client_services.read"),
    ("recurring_orders.update", "client_services.update"),
    ("recurring_orders.update", "client_services.suspend"),
    ("recurring_orders.update", "client_services.reactivate"),
    ("recurring_orders.delete", "client_services.delete"),
    ("recurring_orders.generate", "client_services.generate"),
)

PERMISSION_PREFIXES = ("products", "recurring_orders", "task_states", "workflow_templates")

# (table, column, indexes to drop first)
DROP_COLUMNS = (
    ("order", "recurring_order_id", ("uq_order_active_recurring_due_date",)),
    ("order_item", "product_id", ("idx_order_item_product",)),
    ("service_plan", "product_id", ("uq_service_plan_product",)),
    ("client_service", "recurring_order_id", ()),
    ("task", "task_state_id", ()),
)

DROP_TABLES = ("recurring_order_item", "recurring_order", "product", "task_state", "workflow_template")

# A workflow step / trigger condition naming any of these keys writes or reads a
# dropped column.
LEGACY_KEYS_RE = "(task_state_id|product_id|recurring_order_id)"


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables "
        " WHERE table_schema = 'public' AND table_name = :n"
    ), {"n": name}).scalar())


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        " WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).scalar())


def _grant_copy(conn) -> None:
    copied = 0
    for source, target in GRANT_COPY:
        copied += conn.execute(sa.text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT rp.role_id, pt.id FROM role_permission rp "
            "JOIN permission ps ON ps.id = rp.permission_id AND ps.name = :source "
            "JOIN permission pt ON pt.name = :target "
            "ON CONFLICT DO NOTHING"
        ), {"source": source, "target": target}).rowcount
    print(f"{TAG} grant-copy: {copied} role_permission row(s) added")


# ACTIVE recurring orders no client_service bills (no bridged service carrying
# billing_status). Single source for the migrate step and its assert.
_UNBRIDGED = """
    FROM recurring_order ro
    WHERE ro.status = 'ACTIVE'
      AND NOT EXISTS (
          SELECT 1 FROM client_service cs
           WHERE cs.recurring_order_id = ro.id AND cs.billing_status IS NOT NULL
      )
"""


def _migrate_unbridged(conn) -> None:
    if not (_table_exists(conn, "recurring_order") and _column_exists(conn, "client_service", "recurring_order_id")):
        return
    # Safe-to-migrate rows: a client, exactly one template item, a plan bridged
    # to that item's product. Anything else is reported and aborts the release.
    bad = conn.execute(sa.text(f"""
        SELECT ro.id,
               ro.client_id IS NULL AS no_client,
               (SELECT COUNT(*) FROM recurring_order_item x WHERE x.recurring_order_id = ro.id) AS n_items,
               (SELECT COUNT(*) FROM recurring_order_item x
                  JOIN service_plan sp ON sp.product_id = x.product_id
                 WHERE x.recurring_order_id = ro.id) AS n_plans
        {_UNBRIDGED}
          AND (ro.client_id IS NULL
               OR (SELECT COUNT(*) FROM recurring_order_item x WHERE x.recurring_order_id = ro.id) <> 1
               OR NOT EXISTS (SELECT 1 FROM recurring_order_item x
                                JOIN service_plan sp ON sp.product_id = x.product_id
                               WHERE x.recurring_order_id = ro.id))
    """)).fetchall()
    if bad:
        listed = "; ".join(
            f"{r[0]} (client={'MISSING' if r[1] else 'ok'}, items={r[2]}, plans={r[3]})" for r in bad
        )
        raise RuntimeError(
            f"{TAG} {len(bad)} ACTIVE recurring_order(s) cannot be migrated to a "
            f"client_service safely: {listed}. Each needs a client, exactly one "
            f"item and a service_plan bridged to that item's product. Fix or "
            f"cancel them, then re-run. Nothing was dropped."
        )

    created = conn.execute(sa.text(f"""
        INSERT INTO client_service (
            id, created_at, updated_at, status, activation_date, cancelled_at,
            connection_params, notes, company_id, client_id, service_plan_id,
            recurring_order_id,
            recurrence, recurrence_end, next_generation_date, last_generated_at,
            billing_status, quantity, migration_source
        )
        SELECT
            gen_random_uuid(), ro.created_at, now(), 'ACTIVE'::clientservicestatus,
            ro.created_at, NULL, NULL, NULL, ro.company_id, ro.client_id, sp.id,
            ro.id,
            ro.recurrence, ro.recurrence_end, ro.next_generation_date, ro.last_generated_at,
            ro.status, roi.quantity, 'ld1'
        FROM recurring_order ro
        JOIN recurring_order_item roi ON roi.recurring_order_id = ro.id
        JOIN service_plan sp ON sp.product_id = roi.product_id
        WHERE ro.status = 'ACTIVE'
          AND NOT EXISTS (
              SELECT 1 FROM client_service cs
               WHERE cs.recurring_order_id = ro.id AND cs.billing_status IS NOT NULL
          )
    """)).rowcount
    print(f"{TAG} migrated {created} unbridged ACTIVE recurring_order(s) into client_service")

    # Repoint past orders (c2b Pass 3 rule: exactly one service per legacy row).
    repointed = conn.execute(sa.text("""
        UPDATE "order" o SET client_service_id = cs.id
          FROM client_service cs
         WHERE cs.recurring_order_id = o.recurring_order_id
           AND o.recurring_order_id IS NOT NULL
           AND o.client_service_id IS NULL
           AND (SELECT COUNT(*) FROM client_service c2
                 WHERE c2.recurring_order_id = o.recurring_order_id) = 1
    """)).rowcount
    print(f"{TAG} repointed {repointed} order(s) to their client_service")

    left = conn.execute(sa.text(f"SELECT COUNT(*) {_UNBRIDGED}")).scalar()
    if left:
        raise RuntimeError(f"{TAG} {left} unbridged ACTIVE recurring_order(s) remain after migration")


def _delete_legacy_workflows(conn) -> None:
    ids = {r[0] for r in conn.execute(sa.text(
        "SELECT DISTINCT workflow_id FROM workflow_trigger "
        " WHERE resource_type IN ('recurring_order', 'product', 'task_state') "
        "    OR field_conditions::text ~ :re"
    ), {"re": LEGACY_KEYS_RE}).fetchall()}
    ids |= {r[0] for r in conn.execute(sa.text(
        "SELECT DISTINCT workflow_id FROM workflow_step WHERE action_config::text ~ :re"
    ), {"re": LEGACY_KEYS_RE}).fetchall()}
    if ids:
        names = [r[0] for r in conn.execute(sa.text(
            "SELECT name FROM workflow WHERE id = ANY(:ids)"), {"ids": list(ids)})]
        conn.execute(sa.text("DELETE FROM workflow WHERE id = ANY(:ids)"), {"ids": list(ids)})
        print(f"{TAG} deleted {len(ids)} legacy workflow(s): {names}")
    else:
        print(f"{TAG} deleted 0 legacy workflows")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("SET lock_timeout = '5s'"))

    # a. grant-copy (source permissions still exist)
    _grant_copy(conn)

    # b. migrate unbridged ACTIVE recurring orders
    _migrate_unbridged(conn)

    # tasks / templates linked to the removed enum member
    nulled = conn.execute(sa.text(
        "UPDATE task SET linked_object_type = NULL, linked_object_id = NULL "
        "WHERE linked_object_type = 'RECURRING_ORDER'")).rowcount
    nulled_t = conn.execute(sa.text(
        "UPDATE task_template SET linked_object_type = NULL "
        "WHERE linked_object_type = 'RECURRING_ORDER'")).rowcount
    print(f"{TAG} nulled RECURRING_ORDER links: task={nulled}, task_template={nulled_t}")

    # c. workflows
    _delete_legacy_workflows(conn)

    # d. permissions (role_permission cascades)
    removed = conn.execute(sa.text(
        "DELETE FROM permission WHERE name ~ '^(products|recurring_orders|task_states|workflow_templates)\\.'"
    )).rowcount
    print(f"{TAG} deleted {removed} permission row(s)")

    # e. columns (+ indexes)
    for table, column, indexes in DROP_COLUMNS:
        if _column_exists(conn, table, column):
            for ix in indexes:
                conn.execute(sa.text(f'DROP INDEX IF EXISTS "{ix}"'))
            op.drop_column(table, column)

    # f. tables
    for table in DROP_TABLES:
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{table}"'))
    conn.execute(sa.text("DROP TYPE IF EXISTS taskstatecolor"))

    # g. asserts
    for table in DROP_TABLES:
        assert not _table_exists(conn, table), f"{TAG} table {table} still exists"
    for table, column, _ in DROP_COLUMNS:
        assert not _column_exists(conn, table, column), f"{TAG} {table}.{column} still exists"
    left = conn.execute(sa.text(
        "SELECT COUNT(*) FROM permission WHERE name ~ '^(products|recurring_orders|task_states|workflow_templates)\\.'"
    )).scalar()
    assert left == 0, f"{TAG} {left} legacy permission(s) remain"


def downgrade() -> None:
    raise NotImplementedError(
        "ld1_legacy_drop is not reversible. The product catalog, recurring-order "
        "templates, task columns and workflow-template catalog are dropped, "
        "unbridged recurring orders were folded into client_service, and legacy "
        "workflows were deleted. Restore from a backup taken before the release."
    )
