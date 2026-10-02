"""RBAC: collapse the built-in roles to ADMIN / VIEWER / COLLECTOR / TECHNICIAN

Revision ID: rr1_four_builtin_roles
Revises: ci1_category_icons
Create Date: 2026-10-02

Product decision (2026-10-02): the global (company_id IS NULL) roles are
reduced to four. Tenant custom roles are untouched and stay fully supported.

  ADMIN       full access (the `*` wildcard)
  VIEWER      NEW — every `*.read` permission + web.access, no mobile.*
  COLLECTOR   unchanged grants — cobros app only (no web.access)
  TECHNICIAN  unchanged grants — tecnicos app only (no web.access)

Removed global roles and where their holders land (user_role,
user_invitation_role, notification.pending_role_ids):

  MANAGER -> ADMIN          SALES, USER, NOC, WAREHOUSE, SUPPORT -> VIEWER
  BILLING -> COLLECTOR

`web.access` is a NEW permission gating the web dashboard (frontend-erp
middleware). It is granted to VIEWER and to every existing tenant custom
role, so no custom-role holder loses the web app; tenants untick it in the
role matrix for mobile-only custom roles. ADMIN holds it via `*`.

A tenant custom role whose name collides (case-insensitively) with a built-in
name is renamed "<name> (custom)": name-based ADMIN checks now only honour the
global role, and auth-erp rejects such names going forward.

The seeds (rbac_seed / isp_seed) were edited in the same commit to stop
re-creating the removed roles — they run after every alembic command.

Downgrade restores the seven role rows (USER with its original read grants;
the others empty) and moves VIEWER holders back to USER. It can NOT restore
who held MANAGER/SALES/NOC/... — the remap is one-way.

Hand-written, house style: lock_timeout, idempotent, post-upgrade asserts.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "rr1_four_builtin_roles"
down_revision: Union[str, Sequence[str], None] = "ci1_category_icons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILTIN_ROLES = ("ADMIN", "VIEWER", "COLLECTOR", "TECHNICIAN")

# old global role -> new global role
ROLE_REMAP = {
    "MANAGER": "ADMIN",
    "SALES": "VIEWER",
    "USER": "VIEWER",
    "NOC": "VIEWER",
    "WAREHOUSE": "VIEWER",
    "SUPPORT": "VIEWER",
    "BILLING": "COLLECTOR",
}

# Pinned against rbac_seed.PERMISSIONS_DATA by tests/test_four_builtin_roles.py.
WEB_ACCESS = {
    "name": "web.access",
    "resource": "web",
    "action": "access",
    "description": "Acceso a la aplicación web",
}

VIEWER_DESCRIPTION = "Read access to everything on the web app"

# rbac_seed's original USER grant list, for the downgrade only.
_LEGACY_USER_GRANTS = (
    "clients.read", "orders.read", "payments.read", "products.read",
    "recurring_orders.read", "dashboard.read", "tasks.read", "task_states.read",
)


def _global_role_ids(connection) -> dict:
    rows = connection.execute(text(
        "SELECT name, id FROM role WHERE company_id IS NULL"
    )).fetchall()
    return {name: role_id for name, role_id in rows}


def _ensure_global_role(connection, name: str, description: str):
    row = connection.execute(
        text("SELECT id FROM role WHERE name = :name AND company_id IS NULL"),
        {"name": name},
    ).fetchone()
    if row:
        return row[0]
    return connection.execute(
        text(
            "INSERT INTO role (id, created_at, name, description, is_system) "
            "VALUES (gen_random_uuid(), NOW(), :name, :description, TRUE) RETURNING id"
        ),
        {"name": name, "description": description},
    ).fetchone()[0]


def _move_holders(connection, old_id, new_id) -> None:
    """Re-point every assignment of old_id to new_id (deduplicated)."""
    params = {"old": old_id, "new": new_id}
    connection.execute(text(
        "INSERT INTO user_role (user_id, role_id) "
        "SELECT user_id, :new FROM user_role WHERE role_id = :old "
        "ON CONFLICT DO NOTHING"
    ), params)
    connection.execute(text("DELETE FROM user_role WHERE role_id = :old"), params)
    connection.execute(text(
        "INSERT INTO user_invitation_role (invitation_id, role_id) "
        "SELECT invitation_id, :new FROM user_invitation_role WHERE role_id = :old "
        "ON CONFLICT DO NOTHING"
    ), params)
    connection.execute(text("DELETE FROM user_invitation_role WHERE role_id = :old"), params)


def _remap_pending_role_ids(connection, id_map: dict) -> None:
    """notification.pending_role_ids is a JSON list of role-id strings."""
    str_map = {str(k): str(v) for k, v in id_map.items()}
    rows = connection.execute(text(
        "SELECT id, pending_role_ids FROM notification WHERE pending_role_ids IS NOT NULL"
    )).fetchall()
    for notif_id, ids in rows:
        if isinstance(ids, str):
            ids = json.loads(ids)
        if not ids:
            continue
        new_ids = list(dict.fromkeys(str_map.get(str(i), str(i)) for i in ids))
        if new_ids != [str(i) for i in ids]:
            connection.execute(
                text("UPDATE notification SET pending_role_ids = CAST(:ids AS JSON) WHERE id = :id"),
                {"ids": json.dumps(new_ids), "id": notif_id},
            )


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # 1. web.access permission.
    connection.execute(
        text(
            "INSERT INTO permission (id, created_at, name, resource, action, description) "
            "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        WEB_ACCESS,
    )

    # 2. VIEWER: every read permission + web.access. (The seed's convergent
    #    pass keeps future *.read permissions flowing to it.)
    viewer_id = _ensure_global_role(connection, "VIEWER", VIEWER_DESCRIPTION)
    connection.execute(text(
        "INSERT INTO role_permission (role_id, permission_id) "
        "SELECT :role_id, p.id FROM permission p "
        "WHERE p.action = 'read' OR p.name = 'web.access' "
        "ON CONFLICT DO NOTHING"
    ), {"role_id": viewer_id})

    # 3. Existing tenant custom roles keep the web app.
    connection.execute(text(
        "INSERT INTO role_permission (role_id, permission_id) "
        "SELECT r.id, p.id FROM role r, permission p "
        "WHERE r.company_id IS NOT NULL AND p.name = 'web.access' "
        "ON CONFLICT DO NOTHING"
    ))

    # 4. Rename custom roles that impersonate a built-in name.
    connection.execute(text(
        "UPDATE role SET name = name || ' (custom)' "
        "WHERE company_id IS NOT NULL AND UPPER(name) = ANY(:names)"
    ), {"names": list(BUILTIN_ROLES)})

    # 5. Move holders off the removed roles, then delete them
    #    (role_permission / user_role / user_invitation_role cascade).
    for name in ("ADMIN", "COLLECTOR", "TECHNICIAN"):
        _ensure_global_role(connection, name, f"{name} (built-in)")
    ids = _global_role_ids(connection)
    id_map = {}
    for old, new in ROLE_REMAP.items():
        if old in ids:
            _move_holders(connection, ids[old], ids[new])
            id_map[ids[old]] = ids[new]
    _remap_pending_role_ids(connection, id_map)
    connection.execute(
        text("DELETE FROM role WHERE company_id IS NULL AND name = ANY(:names)"),
        {"names": list(ROLE_REMAP)},
    )

    # Post-upgrade assertions.
    remaining = sorted(_global_role_ids(connection))
    if remaining != sorted(BUILTIN_ROLES):
        raise RuntimeError(f"[rr1] expected global roles {sorted(BUILTIN_ROLES)}, found {remaining}")
    leftover = connection.execute(text(
        "SELECT COUNT(*) FROM role WHERE company_id IS NOT NULL AND UPPER(name) = ANY(:names)"
    ), {"names": list(BUILTIN_ROLES)}).scalar()
    if leftover:
        raise RuntimeError(f"[rr1] {leftover} custom role(s) still use a built-in name")

    print("[rr1_four_builtin_roles] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for name in ROLE_REMAP:
        _ensure_global_role(connection, name, f"{name} (restored by rr1 downgrade)")
    ids = _global_role_ids(connection)

    connection.execute(text(
        "INSERT INTO role_permission (role_id, permission_id) "
        "SELECT :role_id, p.id FROM permission p WHERE p.name = ANY(:names) "
        "ON CONFLICT DO NOTHING"
    ), {"role_id": ids["USER"], "names": list(_LEGACY_USER_GRANTS)})

    if "VIEWER" in ids:
        _move_holders(connection, ids["VIEWER"], ids["USER"])
        _remap_pending_role_ids(connection, {ids["VIEWER"]: ids["USER"]})
        connection.execute(text("DELETE FROM role WHERE id = :id"), {"id": ids["VIEWER"]})

    # Same caveat as cfg3: env.py re-runs the seeds after a downgrade, which
    # re-inserts web.access unless the seed edits are reverted too.
    connection.execute(text(
        "DELETE FROM role_permission WHERE permission_id IN "
        "(SELECT id FROM permission WHERE name = 'web.access')"
    ))
    connection.execute(text("DELETE FROM permission WHERE name = 'web.access'"))

    print("[rr1_four_builtin_roles] downgrade complete")
