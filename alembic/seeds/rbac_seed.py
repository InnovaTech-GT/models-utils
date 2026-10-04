"""
RBAC Seed Script - Automatically seeds permissions and roles

This script is automatically run after each Alembic migration to ensure
RBAC data (permissions and roles) exists in the database.

The script is idempotent - it checks for existing data before inserting,
so it's safe to run multiple times.
"""
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.engine import Connection
import logging
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from database_utils.utils.timezone_utils import now_gt

logger = logging.getLogger(__name__)


PERMISSIONS_DATA = [
        # Client permissions
        {"name": "clients.create", "resource": "clients", "action": "create", "description": "Create new clients"},
        {"name": "clients.read", "resource": "clients", "action": "read", "description": "View client information"},
        {"name": "clients.update", "resource": "clients", "action": "update", "description": "Update client information"},
        {"name": "clients.delete", "resource": "clients", "action": "delete", "description": "Delete clients"},

        # Order permissions
        {"name": "orders.create", "resource": "orders", "action": "create", "description": "Create new orders"},
        {"name": "orders.read", "resource": "orders", "action": "read", "description": "View order information"},
        {"name": "orders.update", "resource": "orders", "action": "update", "description": "Update order information"},
        {"name": "orders.delete", "resource": "orders", "action": "delete", "description": "Delete orders"},
        {"name": "orders.revert_payment", "resource": "orders", "action": "revert_payment", "description": "Revert order payment and invalidate invoice (ADMIN only)"},
        {"name": "orders.change_status", "resource": "orders", "action": "change_status", "description": "Change order status (cancel/activate) - ADMIN only"},

        # Payment ledger permissions (doc 16 §1; also inserted by migration
        # c1b_backfill with grant-copy from the orders.* equivalents)
        {"name": "payments.record", "resource": "payments", "action": "record", "description": "Record payments against orders"},
        {"name": "payments.read", "resource": "payments", "action": "read", "description": "View order payment ledgers"},
        {"name": "payments.refund", "resource": "payments", "action": "refund", "description": "Refund recorded payments (ADMIN only)"},

        # User management permissions
        {"name": "users.create", "resource": "users", "action": "create", "description": "Create new users"},
        {"name": "users.read", "resource": "users", "action": "read", "description": "View user information"},
        {"name": "users.update", "resource": "users", "action": "update", "description": "Update user information"},
        {"name": "users.delete", "resource": "users", "action": "delete", "description": "Delete users"},

        # Role management permissions
        {"name": "roles.create", "resource": "roles", "action": "create", "description": "Create new roles"},
        {"name": "roles.read", "resource": "roles", "action": "read", "description": "View role information"},
        {"name": "roles.update", "resource": "roles", "action": "update", "description": "Update role information"},
        {"name": "roles.delete", "resource": "roles", "action": "delete", "description": "Delete roles"},

        # Permission management permissions
        {"name": "permissions.create", "resource": "permissions", "action": "create", "description": "Create new permissions"},
        {"name": "permissions.read", "resource": "permissions", "action": "read", "description": "View permission information"},
        {"name": "permissions.update", "resource": "permissions", "action": "update", "description": "Update permission information"},
        {"name": "permissions.delete", "resource": "permissions", "action": "delete", "description": "Delete permissions"},

        # Company settings permissions
        {"name": "company.read", "resource": "company", "action": "read", "description": "View company settings"},
        {"name": "company.update", "resource": "company", "action": "update", "description": "Update company settings"},

        # Dashboard permissions
        {"name": "dashboard.read", "resource": "dashboard", "action": "read", "description": "View dashboard statistics"},

        # Task permissions
        {"name": "tasks.create", "resource": "tasks", "action": "create", "description": "Create new tasks"},
        {"name": "tasks.read", "resource": "tasks", "action": "read", "description": "View task information"},
        {"name": "tasks.update", "resource": "tasks", "action": "update", "description": "Update task information"},
        {"name": "tasks.delete", "resource": "tasks", "action": "delete", "description": "Delete tasks"},

        # Integration permissions
        {"name": "integrations.create", "resource": "integrations", "action": "create", "description": "Create external API integrations"},
        {"name": "integrations.read", "resource": "integrations", "action": "read", "description": "View external API integrations"},
        {"name": "integrations.update", "resource": "integrations", "action": "update", "description": "Update external API integrations"},
        {"name": "integrations.delete", "resource": "integrations", "action": "delete", "description": "Delete external API integrations"},

        # Mobile app access (cfg3_matrix_permissions). These are the Figma
        # permission-matrix rows "App de técnico" / "App de cobrador" and are
        # enforced server-side in backend-erp on top of the existing
        # tasks.*/payments.* checks. Granted to TECHNICIAN / COLLECTOR via
        # isp_seed.ISP_ROLES; ADMIN converges automatically.
        {"name": "mobile.technician", "resource": "mobile", "action": "technician", "description": "Acceso a la App de técnico"},
        {"name": "mobile.collector", "resource": "mobile", "action": "collector", "description": "Acceso a la App de cobrador"},

        # Activity log (cfg3_matrix_permissions). Replaces the ADMIN role gate
        # on auth-erp's GET /audit-logs.
        {"name": "audit_logs.read", "resource": "audit_logs", "action": "read", "description": "Ver el registro de actividad"},

        # Web dashboard access (rr1_four_builtin_roles). Enforced by
        # frontend-erp's middleware; mobile-only roles (COLLECTOR,
        # TECHNICIAN) don't hold it. Pinned against rr1.WEB_ACCESS.
        {"name": "web.access", "resource": "web", "action": "access", "description": "Acceso a la aplicación web"},

        # Cash-box review (cr1_cash_review): admins approve/reject the boxes
        # collectors submit. ADMIN converges via the `*` step; no other
        # built-in role holds it (tenants can add it to custom roles).
        {"name": "cash_sessions.review", "resource": "cash_sessions", "action": "review", "description": "Revisar, aprobar o rechazar cajas de cobradores"},
]

# The four built-in (global) roles. ADMIN/VIEWER are created here;
# COLLECTOR/TECHNICIAN by isp_seed.ISP_ROLES. Tenants add custom roles on top.
VIEWER_PERMISSION_FILTER = "p.action = 'read' OR p.name = 'web.access'"


def seed_rbac_data(connection: Connection) -> None:
    """
    Seed RBAC permissions and roles into the database.

    This function is idempotent - it checks if data exists before inserting.
    Safe to run multiple times.

    Args:
        connection: SQLAlchemy connection object
    """
    try:
        # Check if RBAC tables exist
        tables_exist = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name IN ('permission', 'role', 'role_permission', 'user_role')
                """
            )
        ).scalar()

        if tables_exist < 4:
            logger.info("RBAC tables do not exist yet. Skipping seed.")
            return

        # Check if roles already exist
        existing_roles = connection.execute(
            text("SELECT COUNT(*) FROM role")
        ).scalar()

        if existing_roles > 0:
            logger.info(f"RBAC data already seeded ({existing_roles} roles found). Reconciling additively.")
            _ensure_convergent_rbac(connection)
            connection.commit()
            return

        logger.info("Seeding RBAC permissions and roles...")

        # 1. Seed default permissions
        permissions_data = PERMISSIONS_DATA

        for perm in permissions_data:
            connection.execute(
                text(
                    "INSERT INTO permission (id, created_at, name, resource, action, description) "
                    "VALUES (gen_random_uuid(), :created_at, :name, :resource, :action, :description) "
                    # Some permissions are also inserted by individual migrations
                    # (e.g. orders.change_status, integrations.*). On a from-scratch
                    # DB the seed runs while `role` is still empty, so it must not
                    # collide with those migration-inserted rows.
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {
                    'created_at': now_gt(),
                    'name': perm['name'],
                    'resource': perm['resource'],
                    'action': perm['action'],
                    'description': perm['description']
                }
            )

        logger.info(f"✓ Seeded {len(permissions_data)} permissions")

        # 2. Create default roles
        roles_data = [
            {"name": "ADMIN", "description": "Administrator with all permissions", "is_system": True},
            {"name": "VIEWER", "description": "Read access to everything on the web app", "is_system": True},
        ]

        role_ids = {}
        for role_data in roles_data:
            result = connection.execute(
                text(
                    "INSERT INTO role (id, created_at, name, description, is_system) "
                    "VALUES (gen_random_uuid(), :created_at, :name, :description, :is_system) "
                    "RETURNING id"
                ),
                {
                    'created_at': now_gt(),
                    'name': role_data['name'],
                    'description': role_data['description'],
                    'is_system': role_data['is_system']
                }
            )
            role_ids[role_data['name']] = result.fetchone()[0]

        logger.info(f"✓ Seeded {len(roles_data)} roles")

        # 3. Assign permissions to roles

        # ADMIN: All permissions
        admin_permissions = connection.execute(
            text("SELECT id FROM permission")
        ).fetchall()

        for perm_row in admin_permissions:
            connection.execute(
                text(
                    "INSERT INTO role_permission (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id)"
                ),
                {
                    'role_id': role_ids['ADMIN'],
                    'permission_id': perm_row[0]
                }
            )

        logger.info(f"✓ ADMIN role assigned {len(admin_permissions)} permissions")

        # VIEWER's grants (every read permission + web.access) are applied by
        # _ensure_convergent_rbac below, so future read permissions flow too.

        # 4. Migrate existing users to new role system (if any exist)
        # Skip legacy admin column migration - this is no longer needed with UUID migration
        logger.info("Skipping legacy user migration (fresh database with UUID schema)")

        _ensure_convergent_rbac(connection)
        connection.commit()
        logger.info("✓ RBAC seed completed successfully!")

    except Exception as e:
        logger.error(f"Error seeding RBAC data: {e}")
        raise


def _ensure_convergent_rbac(connection: Connection) -> None:
    """Additive reconciliation, run on EVERY migrate (fresh or existing DB).

    The historical guard skipped the whole seed once any role existed, so a
    database seeded before a permission was added to PERMISSIONS_DATA never
    received it — e.g. production lacked orders.revert_payment, which meant
    migration c1b's grant-copy gave payments.refund to no role at all.

    Only INSERT ... ON CONFLICT DO NOTHING — never UPDATE or DELETE — so
    tenant-specific grant customizations are preserved.
    """
    # 1. Every defined base permission exists.
    for perm in PERMISSIONS_DATA:
        connection.execute(
            text(
                "INSERT INTO permission (id, created_at, name, resource, action, description) "
                "VALUES (gen_random_uuid(), :created_at, :name, :resource, :action, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"created_at": now_gt(), **perm},
        )

    # 2. Global system ADMIN holds every permission.
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r CROSS JOIN permission p "
            "WHERE r.name = 'ADMIN' AND r.company_id IS NULL "
            "ON CONFLICT DO NOTHING"
        )
    )

    # 3. Global system VIEWER holds every read permission + web.access.
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r CROSS JOIN permission p "
            "WHERE r.name = 'VIEWER' AND r.company_id IS NULL "
            f"AND ({VIEWER_PERMISSION_FILTER}) "
            "ON CONFLICT DO NOTHING"
        )
    )

    # 4. Ledger grants derived from existing order grants (mirrors migration
    #    c1b's copy rule, but convergent: roles created AFTER c1b — e.g. by
    #    isp_seed — pick these up on the next migrate instead of never).
    #
    #    The legacy products.*/recurring_orders.* -> service_plans.*/
    #    client_services.* copy pairs were applied once by ld1_legacy_drop
    #    (which then deleted the source permissions) and are gone from here.
    #
    #    client_services.adopt deliberately has NO legacy source (doc 30):
    #    adoption is a new ADMIN-only capability, never inherited from
    #    recurring_orders grants.
    for source, target in (
        ("orders.update", "payments.record"),
        ("orders.read", "payments.read"),
        ("orders.revert_payment", "payments.refund"),
    ):
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT rp.role_id, pt.id "
                "FROM role_permission rp "
                "JOIN permission ps ON ps.id = rp.permission_id AND ps.name = :source "
                "JOIN permission pt ON pt.name = :target "
                "ON CONFLICT DO NOTHING"
            ),
            {"source": source, "target": target},
        )

    logger.info("✓ RBAC convergent reconciliation done")
