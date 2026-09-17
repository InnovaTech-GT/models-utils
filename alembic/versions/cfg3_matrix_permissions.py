"""Configuración/Roles y permisos: mobile.technician, mobile.collector, audit_logs.read

Revision ID: cfg3_matrix_permissions
Revises: cfg2_service_plan_group
Create Date: 2026-09-15

Figma redesign PR 2 (docs/design/plans/02-configuracion.md §2.3, master plan
§2.5), third of three. The Figma permission matrix has three rows nothing in
the database can express today:

  - "App de técnico"  -> mobile.technician  (granted to TECHNICIAN)
  - "App de cobrador" -> mobile.collector   (granted to BILLING)
  - "Actividad"       -> audit_logs.read    (granted to ADMIN/MANAGER only)

ONLY THESE THREE ROWS ARE NEW. Every other permission the matrix maps —
`insights.*` in particular (isp_seed.py, Cycle 4) — already exists and is
reused unchanged. Do not re-seed anything else here.

ADMIN and MANAGER get all three with NO per-role edit anywhere:
_ensure_convergent_rbac's ADMIN cross-join (rbac_seed step 2) and MANAGER
cross-join minus resources roles/permissions/company minus
MANAGER_EXCLUDED_PERMISSIONS (step 3) both pick them up on the next migrate.
Q3 is resolved: MANAGER KEEPS audit_logs.read, so the name goes in NEITHER
rbac_seed.MANAGER_EXCLUDED_PERMISSIONS NOR isp_seed.ADMIN_ONLY_PERMISSIONS.

This revision carries the seed edits that ship in the same commit
(rbac_seed.PERMISSIONS_DATA rows + two isp_seed.ISP_ROLES list entries) —
`migrate.yml` is path-filtered on alembic/**, so a seed change without a
revision never reaches production. PERMISSIONS below is pinned byte-identical
to the seed rows by tests/test_matrix_permissions_seed.py: the seeds are two
files with two different shapes and editing only one half is the easy
mistake.

`mobile.*` is enforced server-side by backend-erp in this same cycle (Q4,
user decision 2026-09-15 — no production users yet, nobody gets locked out).

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, idempotent
INSERT ... ON CONFLICT DO NOTHING, post-upgrade assertions, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'cfg3_matrix_permissions'
down_revision: Union[str, Sequence[str], None] = 'cfg2_service_plan_group'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against alembic/seeds/rbac_seed.py by
# tests/test_matrix_permissions_seed.py (revisions are immutable, seeds are
# not — neither can import the other; ba1 precedent).
PERMISSIONS = [
    {"name": "mobile.technician", "resource": "mobile", "action": "technician", "description": "Acceso a la App de técnico"},
    {"name": "mobile.collector", "resource": "mobile", "action": "collector", "description": "Acceso a la App de cobrador"},
    {"name": "audit_logs.read", "resource": "audit_logs", "action": "read", "description": "Ver el registro de actividad"},
]

# Grants this revision makes explicitly. ADMIN/MANAGER are NOT listed: the
# convergent reconciler grants them, and duplicating that here would be a
# second source of truth to keep in sync.
# Pinned against isp_seed.ISP_ROLES by the same test.
GRANTS = (
    ("TECHNICIAN", "mobile.technician"),
    ("BILLING", "mobile.collector"),
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for perm in PERMISSIONS:
        connection.execute(
            text(
                "INSERT INTO permission (id, created_at, name, resource, action, description) "
                "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            perm,
        )

    for role_name, perm_name in GRANTS:
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = :role AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"role": role_name, "perm": perm_name},
        )

    # ADMIN/MANAGER convergence runs in env.py's post-upgrade seed pass, so it
    # cannot be asserted here — only that the rows themselves landed.
    names = ", ".join(f"'{p['name']}'" for p in PERMISSIONS)
    found = connection.execute(text(
        f"SELECT COUNT(*) FROM permission WHERE name IN ({names})"
    )).scalar()
    if found != len(PERMISSIONS):
        raise RuntimeError(
            f"[cfg3] expected {len(PERMISSIONS)} permission rows after upgrade, found {found}"
        )

    print("[cfg3_matrix_permissions] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Drops every grant, including the ADMIN/MANAGER ones the seed converged
    # and any a tenant added to a custom role — the permission row is going
    # away, so nothing can be left pointing at it.
    #
    # Caveat (pre-existing, ba1 has it too): alembic/env.py runs the seeds
    # after EVERY online alembic command, downgrades included, so a downgrade
    # run through env.py re-inserts these rows (with the ADMIN/MANAGER grants)
    # the moment the seed pass follows. The revision is still reversible — it
    # undoes exactly what it did; the seed is a separate, always-converging
    # writer. To genuinely remove the rows, revert the seed edits too.
    names = ", ".join(f"'{p['name']}'" for p in PERMISSIONS)
    connection.execute(text(
        "DELETE FROM role_permission WHERE permission_id IN "
        f"(SELECT id FROM permission WHERE name IN ({names}))"
    ))
    connection.execute(text(f"DELETE FROM permission WHERE name IN ({names})"))

    print("[cfg3_matrix_permissions] downgrade complete")
