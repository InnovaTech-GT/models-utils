"""Fixed task status: task.status replaces the per-tenant board columns

Revision ID: ts1_task_status
Revises: tr1_transport_axis
Create Date: 2026-09-28

Uplink stops being a generic ERP here: a task's status is one of four fixed
values for every tenant, PENDING, ASSIGNED, IN_PROGRESS and DONE, instead of a
tenant-defined `task_state` row. PENDING means no technician yet and ASSIGNED
means it has one; both follow the assignment (utils/task_status.py). The
dispatch-routes ETL routes PENDING and ASSIGNED work and needs these values to
mean the same thing in every tenant.

1. `task.status` VARCHAR(20) NOT NULL DEFAULT 'PENDING', CHECK-constrained
   (ck_task_status, a string and not a PG enum, same precedent as
   ck_task_state_kind), plus ix_task_company_status for the list, stats and
   dispatch queries.

   Backfill from the old column's kind: CANCELLED and DONE become DONE,
   IN_PROGRESS stays, and ASSIGNED becomes ASSIGNED when the task has a
   technician (task_assignee role TECHNICIAN, or a legacy NULL role) and
   PENDING otherwise.

2. `task.task_state_id` becomes nullable. Nothing is dropped in this revision:
   the task_state table, the FK and the task_states.* permissions stay until
   every consumer reads `status`, and go in a later, destructive revision.

3. Installed workflows are rewritten, because they hold task_state UUIDs that
   stop meaning anything once writers move to `status`:
   - task triggers on `task_state_id` become triggers on `status`, with the
     value mapped through that state's kind;
   - CREATE_TASK steps (and task data/updates in generic steps) carrying a
     `task_state_id` get the mapped `status` instead.
   A UUID that does not match any task_state row is left untouched. The
   engine still accepts a legacy `task_state_id`, so a workflow this pass
   could not map keeps working.

Seed edits ride this revision (the prod migrate workflow is path-filtered on
alembic/**): the new-installation and installation-provisioning templates
drop their board-column parameters and speak `status`.

Hand-written, tk2 house style: lock_timeout, IF NOT EXISTS, post-upgrade
assertions, total downgrade.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "ts1_task_status"
down_revision: Union[str, Sequence[str], None] = "tr1_transport_axis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors database_utils.models.crm.TASK_STATUSES / _TASK_STATUS_CHECK.
TASK_STATUSES = ("PENDING", "ASSIGNED", "IN_PROGRESS", "DONE")
_TASK_STATUS_CHECK = "status IN ('PENDING','ASSIGNED','IN_PROGRESS','DONE')"

# Mirrors database_utils.utils.task_status.STATUS_FROM_STATE_KIND.
STATUS_FROM_STATE_KIND = {
    "ASSIGNED": "ASSIGNED",
    "IN_PROGRESS": "IN_PROGRESS",
    "DONE": "DONE",
    "CANCELLED": "DONE",
}

# Downgrade only: the column a status goes back to.
_KIND_FROM_STATUS = {
    "PENDING": "ASSIGNED",
    "ASSIGNED": "ASSIGNED",
    "IN_PROGRESS": "IN_PROGRESS",
    "DONE": "DONE",
}

_DEFAULT_STATES = (
    ("Asignadas", "ASSIGNED", 0, "BLUE"),
    ("En proceso", "IN_PROGRESS", 1, "ORANGE"),
    ("Finalizadas", "DONE", 2, "GREEN"),
)


def _rewrite(obj, mapping, from_key, to_key):
    """Replace obj[from_key] by obj[to_key] = mapping[value]. Returns True if changed."""
    if not isinstance(obj, dict) or from_key not in obj:
        return False
    mapped = mapping.get(str(obj[from_key]))
    if mapped is None:
        return False
    obj.pop(from_key)
    obj.setdefault(to_key, mapped)
    return True


def _rewrite_config(config, mapping, from_key, to_key):
    changed = _rewrite(config, mapping, from_key, to_key)
    for nested in ("data", "updates"):
        if isinstance(config, dict):
            changed = _rewrite(config.get(nested), mapping, from_key, to_key) or changed
    return changed


def _as_dict(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. task.status -----------------------------------------------------
    op.execute(
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS "
        "status VARCHAR(20) NOT NULL DEFAULT 'PENDING'"
    )
    connection.execute(text(
        "UPDATE task t SET status = CASE ts.kind "
        "  WHEN 'CANCELLED' THEN 'DONE' "
        "  WHEN 'DONE' THEN 'DONE' "
        "  WHEN 'IN_PROGRESS' THEN 'IN_PROGRESS' "
        "  ELSE CASE WHEN EXISTS ("
        "    SELECT 1 FROM task_assignee a WHERE a.task_id = t.id "
        "    AND (a.role IS NULL OR a.role = 'TECHNICIAN')"
        "  ) THEN 'ASSIGNED' ELSE 'PENDING' END "
        "END "
        "FROM task_state ts WHERE ts.id = t.task_state_id"
    ))
    op.execute("ALTER TABLE task DROP CONSTRAINT IF EXISTS ck_task_status")
    op.execute(f"ALTER TABLE task ADD CONSTRAINT ck_task_status CHECK ({_TASK_STATUS_CHECK})")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_company_status ON task (company_id, status)"
    )

    # --- 2. task.task_state_id nullable ---------------------------------------
    op.execute("ALTER TABLE task ALTER COLUMN task_state_id DROP NOT NULL")

    # --- 3. installed workflows ------------------------------------------------
    state_status = {
        str(row.id): STATUS_FROM_STATE_KIND[row.kind]
        for row in connection.execute(text("SELECT id, kind FROM task_state"))
    }

    triggers = 0
    for row in connection.execute(text(
        "SELECT id, field_conditions FROM workflow_trigger "
        "WHERE resource_type = 'task' AND field_conditions IS NOT NULL"
    )).fetchall():
        conditions = _as_dict(row.field_conditions)
        if not isinstance(conditions, dict) or conditions.get("field") != "task_state_id":
            continue
        mapped = state_status.get(str(conditions.get("value")))
        if mapped is None:
            continue
        conditions["field"] = "status"
        conditions["value"] = mapped
        connection.execute(
            text("UPDATE workflow_trigger SET field_conditions = CAST(:c AS JSON) WHERE id = :id"),
            {"c": json.dumps(conditions), "id": row.id},
        )
        triggers += 1

    steps = 0
    for row in connection.execute(text(
        "SELECT id, action_config FROM workflow_step "
        "WHERE action_config::text LIKE '%task_state_id%'"
    )).fetchall():
        config = _as_dict(row.action_config)
        if _rewrite_config(config, state_status, "task_state_id", "status"):
            connection.execute(
                text("UPDATE workflow_step SET action_config = CAST(:c AS JSON) WHERE id = :id"),
                {"c": json.dumps(config), "id": row.id},
            )
            steps += 1

    print(f"[ts1_task_status] rewrote {triggers} trigger(s) and {steps} step(s)")

    # --- assertions ---------------------------------------------------------
    nullable = connection.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'task' AND column_name = 'status'"
    )).scalar()
    if nullable != "NO":
        raise RuntimeError("[ts1] task.status missing or nullable after upgrade")
    if connection.execute(
        text("SELECT to_regclass('ix_task_company_status')")
    ).scalar() is None:
        raise RuntimeError("[ts1] expected index 'ix_task_company_status'")
    if connection.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_status'")
    ).scalar() is None:
        raise RuntimeError("[ts1] expected constraint 'ck_task_status'")
    state_nullable = connection.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'task' AND column_name = 'task_state_id'"
    )).scalar()
    if state_nullable != "YES":
        raise RuntimeError("[ts1] task.task_state_id is still NOT NULL")

    print("[ts1_task_status] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Every company with tasks needs a column to go back to.
    companies = connection.execute(text(
        "SELECT DISTINCT t.company_id FROM task t "
        "WHERE t.task_state_id IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM task_state s WHERE s.company_id = t.company_id)"
    )).fetchall()
    for (company_id,) in companies:
        for name, kind, position, color in _DEFAULT_STATES:
            connection.execute(text(
                "INSERT INTO task_state (id, created_at, updated_at, name, color, position, kind, company_id) "
                "VALUES (gen_random_uuid(), now(), now(), :name, :color, :position, :kind, :company)"
            ), {"name": name, "color": color, "position": position, "kind": kind, "company": company_id})

    # The column for (company, status): lowest position with the matching
    # kind, else the company's lowest position.
    def state_for(company_id, status):
        kind = _KIND_FROM_STATUS.get(status, "ASSIGNED")
        return connection.execute(text(
            "SELECT id FROM task_state WHERE company_id = :c "
            "ORDER BY (kind = :k) DESC, position, created_at LIMIT 1"
        ), {"c": company_id, "k": kind}).scalar()

    for company_id, status in connection.execute(text(
        "SELECT DISTINCT company_id, status FROM task WHERE task_state_id IS NULL"
    )).fetchall():
        connection.execute(text(
            "UPDATE task SET task_state_id = :s "
            "WHERE task_state_id IS NULL AND company_id = :c AND status = :st"
        ), {"s": state_for(company_id, status), "c": company_id, "st": status})

    # Workflows back to board columns, per the workflow's company.
    for row in connection.execute(text(
        "SELECT wt.id, wt.field_conditions, w.company_id FROM workflow_trigger wt "
        "JOIN workflow w ON w.id = wt.workflow_id "
        "WHERE wt.resource_type = 'task' AND wt.field_conditions IS NOT NULL"
    )).fetchall():
        conditions = _as_dict(row.field_conditions)
        if not isinstance(conditions, dict) or conditions.get("field") != "status":
            continue
        state_id = state_for(row.company_id, conditions.get("value"))
        if state_id is None:
            continue
        conditions["field"] = "task_state_id"
        conditions["value"] = str(state_id)
        connection.execute(
            text("UPDATE workflow_trigger SET field_conditions = CAST(:c AS JSON) WHERE id = :id"),
            {"c": json.dumps(conditions), "id": row.id},
        )

    for row in connection.execute(text(
        "SELECT ws.id, ws.action_config, w.company_id FROM workflow_step ws "
        "JOIN workflow w ON w.id = ws.workflow_id "
        "WHERE ws.action_type = 'CREATE_TASK'"
    )).fetchall():
        config = _as_dict(row.action_config)
        status = config.get("status") if isinstance(config, dict) else None
        if status is None or "task_state_id" in config:
            continue
        state_id = state_for(row.company_id, status)
        if state_id is None:
            continue
        config.pop("status")
        config["task_state_id"] = str(state_id)
        connection.execute(
            text("UPDATE workflow_step SET action_config = CAST(:c AS JSON) WHERE id = :id"),
            {"c": json.dumps(config), "id": row.id},
        )

    op.execute("ALTER TABLE task ALTER COLUMN task_state_id SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS ix_task_company_status")
    op.execute("ALTER TABLE task DROP CONSTRAINT IF EXISTS ck_task_status")
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS status")
    print("[ts1_task_status] downgrade complete")
