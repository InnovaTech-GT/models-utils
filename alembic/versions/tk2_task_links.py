"""Ordenes de Trabajo: task_state.kind, task FKs, task_assignee.role

Revision ID: tk2_task_links
Revises: tj1_task_job_kinds
Create Date: 2026-09-16

Figma redesign PR 4 (docs/design/plans/04-ordenes-trabajo.md §2.2-§2.5,
master plan §2.1 / §2.7 / §4 row 6). Three independent-looking additions that
have to ship together because the same page reads all three.

1. `task_state.kind` — the SEMANTIC behind free-form column names.
   `Actuales` = kind NOT IN ('DONE','CANCELLED'); `Historico` = the
   complement; the KPI cards and the status chips bucket on it. A
   CHECK-constrained VARCHAR, not a PG enum (client_service.install_state
   precedent): an open set stays a cheap ALTER, and the frontend already
   treats unknown values as "other".

   The backfill is a NAME HEURISTIC and it is only a heuristic: a tenant
   whose columns are named in a way these patterns miss lands everything in
   the NOT NULL default 'IN_PROGRESS', which reads as "KPIs are zero and
   Historico is empty". That is visible, it is not data loss, and it is
   fixable in one place — the toolbar gear -> TaskStateSettingsModal, which
   PATCHes `kind` directly (master plan §9.3).

2. `task.{client_id, client_service_id, device_category_id, inventory_item_id,
   parent_item_id, address}` — the Figma form writes client + service +
   device + parent node SIMULTANEOUSLY, and the single polymorphic
   `linked_object_type`/`linked_object_id` pair cannot hold more than one of
   them. The new FKs become the source of truth; `linked_object_*` is kept in
   sync as a derived compat field (backend `_sync_linked_object`, and the
   workflow engine's CREATE_TASK, which fills the FKs from the polymorphic
   link using the SAME precedence as the backfill below).

   Backfill precedence CLIENT_SERVICE > CLIENT > INVENTORY_ITEM. Known,
   deliberate consequence: a task carrying both a service and a device is
   written as `linked_object_type='CLIENT_SERVICE'`, so consumers filtering
   `linked_object_type == 'INVENTORY_ITEM'` stop seeing device-linked
   installs. Nothing is lost — the device lives in `task.inventory_item_id`.

   Every backfill is a single idempotent UPDATE guarded by `IS NULL` and by a
   join against the target table: `linked_object_id` has no FK, so a row
   pointing at a deleted object must be skipped rather than trip the new
   constraint.

3. `task_assignee.role` — the edit form assigns a technician AND a collector
   to the same task. PK stays (task_id, user_id); legacy rows stay NULL and
   are read as technicians.

Seed edits ride this revision (MU convention: a seed change ships with a
revision, the prod migrate workflow is path-filtered on alembic/**):
`isp_seed.ISP_ROLES['COLLECTOR']` and the new-installation template's
`job_kind: INSTALL`.

Fully reversible: every object created here is dropped by downgrade().
(`tj1`'s enum labels are not, by nature — see that file.)

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertions, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'tk2_task_links'
down_revision: Union[str, Sequence[str], None] = 'tj1_task_job_kinds'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = (
    ("task_state", "kind"),
    ("task", "client_id"),
    ("task", "client_service_id"),
    ("task", "device_category_id"),
    ("task", "inventory_item_id"),
    ("task", "parent_item_id"),
    ("task", "address"),
    ("task_assignee", "role"),
)

_NEW_INDEXES = (
    "ix_task_company_client",
    "ix_task_company_job_kind",
    "ix_task_company_due_date",
)

_NEW_CHECKS = ("ck_task_state_kind", "ck_task_assignee_role")

# Mirrors database_utils.models.crm.TASK_STATE_KINDS.
TASK_STATE_KINDS = ("ASSIGNED", "IN_PROGRESS", "DONE", "CANCELLED")

# name ILIKE patterns -> kind. Order matters: DONE and CANCELLED win over
# ASSIGNED, and ASSIGNED only claims rows still sitting on the default.
_KIND_HEURISTIC = (
    ("DONE", ("%finaliz%", "%complet%", "%cerrad%", "%termin%", "%done%", "%listo%")),
    ("CANCELLED", ("%cancel%", "%anulad%", "%descart%")),
    ("ASSIGNED", ("%asign%", "%pendien%", "%nuev%", "%por hacer%", "%todo%", "%backlog%")),
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. task_state.kind -------------------------------------------------
    op.execute(
        "ALTER TABLE task_state ADD COLUMN IF NOT EXISTS "
        "kind VARCHAR(20) NOT NULL DEFAULT 'IN_PROGRESS'"
    )
    op.execute(
        "ALTER TABLE task_state DROP CONSTRAINT IF EXISTS ck_task_state_kind"
    )
    op.execute(
        "ALTER TABLE task_state ADD CONSTRAINT ck_task_state_kind CHECK ("
        "kind IN ('ASSIGNED','IN_PROGRESS','DONE','CANCELLED'))"
    )

    for kind, patterns in _KIND_HEURISTIC:
        # ASSIGNED runs last and must not steal a row the first two claimed.
        guard = " AND kind = 'IN_PROGRESS'" if kind == "ASSIGNED" else ""
        connection.execute(
            text(
                f"UPDATE task_state SET kind = '{kind}' "
                f"WHERE name ILIKE ANY (:patterns){guard}"
            ),
            {"patterns": list(patterns)},
        )

    # --- 2. task FK columns -------------------------------------------------
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS client_id UUID")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS client_service_id UUID")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS device_category_id UUID")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS inventory_item_id UUID")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS parent_item_id UUID")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS address VARCHAR")

    # ON DELETE: SET NULL everywhere except device_category — that table is
    # platform-global and admin-curated, so deleting a category out from under
    # open work orders is a data bug to block, not a cascade to absorb.
    for name, column, target, on_delete in (
        ("fk_task_client_id", "client_id", "client", "SET NULL"),
        ("fk_task_client_service_id", "client_service_id", "client_service", "SET NULL"),
        ("fk_task_device_category_id", "device_category_id", "device_category", "RESTRICT"),
        ("fk_task_inventory_item_id", "inventory_item_id", "inventory_item", "SET NULL"),
        ("fk_task_parent_item_id", "parent_item_id", "inventory_item", "SET NULL"),
    ):
        op.execute(f"ALTER TABLE task DROP CONSTRAINT IF EXISTS {name}")
        op.execute(
            f"ALTER TABLE task ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {target} (id) ON DELETE {on_delete}"
        )

    # Backfill from the polymorphic link. Precedence CLIENT_SERVICE > CLIENT >
    # INVENTORY_ITEM is encoded by the type predicate — the three sets are
    # disjoint, so the order of the statements does not matter; the precedence
    # only bites in the FORWARD direction (_sync_linked_object / CREATE_TASK).
    # Each joins its target so a dangling linked_object_id is skipped.
    op.execute(
        "UPDATE task t SET client_id = c.id FROM client c "
        "WHERE t.client_id IS NULL AND t.linked_object_type = 'CLIENT' "
        "AND c.id = t.linked_object_id AND c.company_id = t.company_id"
    )
    op.execute(
        "UPDATE task t SET client_service_id = cs.id, "
        "client_id = COALESCE(t.client_id, cs.client_id) "
        "FROM client_service cs "
        "WHERE t.client_service_id IS NULL "
        "AND t.linked_object_type = 'CLIENT_SERVICE' "
        "AND cs.id = t.linked_object_id AND cs.company_id = t.company_id"
    )
    op.execute(
        "UPDATE task t SET inventory_item_id = ii.id, "
        "device_category_id = COALESCE(t.device_category_id, dt.category_id) "
        "FROM inventory_item ii JOIN device_type dt ON dt.id = ii.device_type_id "
        "WHERE t.inventory_item_id IS NULL "
        "AND t.linked_object_type = 'INVENTORY_ITEM' "
        "AND ii.id = t.linked_object_id AND ii.company_id = t.company_id"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_company_client "
        "ON task (company_id, client_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_company_job_kind "
        "ON task (company_id, job_kind)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_company_due_date "
        "ON task (company_id, due_date)"
    )

    # --- 3. task_assignee.role ---------------------------------------------
    op.execute("ALTER TABLE task_assignee ADD COLUMN IF NOT EXISTS role VARCHAR(20)")
    op.execute(
        "ALTER TABLE task_assignee DROP CONSTRAINT IF EXISTS ck_task_assignee_role"
    )
    op.execute(
        "ALTER TABLE task_assignee ADD CONSTRAINT ck_task_assignee_role CHECK ("
        "role IS NULL OR role IN ('TECHNICIAN','COLLECTOR'))"
    )

    # --- assertions ---------------------------------------------------------
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[tk2] expected column(s) missing after upgrade: {missing}")

    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[tk2] expected index '{index_name}' to exist after upgrade"
            )

    for check_name in _NEW_CHECKS:
        if connection.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": check_name},
        ).scalar() is None:
            raise RuntimeError(
                f"[tk2] expected constraint '{check_name}' to exist after upgrade"
            )

    bad = connection.execute(text(
        "SELECT count(*) FROM task_state WHERE kind NOT IN "
        "('ASSIGNED','IN_PROGRESS','DONE','CANCELLED')"
    )).scalar()
    if bad:
        raise RuntimeError(f"[tk2] {bad} task_state row(s) hold an unknown kind")

    print("[tk2_task_links] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        "ALTER TABLE task_assignee DROP CONSTRAINT IF EXISTS ck_task_assignee_role"
    )
    op.execute("ALTER TABLE task_assignee DROP COLUMN IF EXISTS role")

    for index_name in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    # Dropping the columns drops their FK constraints with them.
    for column in (
        "address", "parent_item_id", "inventory_item_id",
        "device_category_id", "client_service_id", "client_id",
    ):
        op.execute(f"ALTER TABLE task DROP COLUMN IF EXISTS {column}")

    op.execute("ALTER TABLE task_state DROP CONSTRAINT IF EXISTS ck_task_state_kind")
    op.execute("ALTER TABLE task_state DROP COLUMN IF EXISTS kind")

    print("[tk2_task_links] downgrade complete")
