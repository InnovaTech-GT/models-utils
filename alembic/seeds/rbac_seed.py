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

        # Product permissions
        {"name": "products.create", "resource": "products", "action": "create", "description": "Create new products"},
        {"name": "products.read", "resource": "products", "action": "read", "description": "View product information"},
        {"name": "products.update", "resource": "products", "action": "update", "description": "Update product information"},
        {"name": "products.delete", "resource": "products", "action": "delete", "description": "Delete products"},

        # Recurring order permissions
        {"name": "recurring_orders.create", "resource": "recurring_orders", "action": "create", "description": "Create recurring orders"},
        {"name": "recurring_orders.read", "resource": "recurring_orders", "action": "read", "description": "View recurring orders"},
        {"name": "recurring_orders.update", "resource": "recurring_orders", "action": "update", "description": "Update recurring orders"},
        {"name": "recurring_orders.delete", "resource": "recurring_orders", "action": "delete", "description": "Delete recurring orders"},

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

        # Task state permissions
        {"name": "task_states.create", "resource": "task_states", "action": "create", "description": "Create task states/columns"},
        {"name": "task_states.read", "resource": "task_states", "action": "read", "description": "View task states/columns"},
        {"name": "task_states.update", "resource": "task_states", "action": "update", "description": "Update task states/columns"},
        {"name": "task_states.delete", "resource": "task_states", "action": "delete", "description": "Delete task states/columns"},

        # Integration permissions
        {"name": "integrations.create", "resource": "integrations", "action": "create", "description": "Create external API integrations"},
        {"name": "integrations.read", "resource": "integrations", "action": "read", "description": "View external API integrations"},
        {"name": "integrations.update", "resource": "integrations", "action": "update", "description": "Update external API integrations"},
        {"name": "integrations.delete", "resource": "integrations", "action": "delete", "description": "Delete external API integrations"},
]

# Permission names withheld from the MANAGER auto-grants (initial seed AND
# _ensure_convergent_rbac step 3). Includes seed-owned ADMIN-only keys from
# other seed modules (isp_seed.ADMIN_ONLY_PERMISSIONS must be a subset —
# pinned by tests/test_attested_adoption.py; seeds cannot import each other).
MANAGER_EXCLUDED_PERMISSIONS = (
    "orders.revert_payment",
    "payments.refund",
    "client_services.adopt",  # ba1: brownfield adoption is ADMIN-only
)
_MANAGER_EXCLUDED_SQL = ", ".join(f"'{n}'" for n in MANAGER_EXCLUDED_PERMISSIONS)


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
            {"name": "MANAGER", "description": "Manager with most permissions except system settings", "is_system": True},
            {"name": "SALES", "description": "Sales representative with client and order permissions", "is_system": True},
            {"name": "USER", "description": "Basic user with read-only permissions", "is_system": True},
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

        # MANAGER: All permissions except roles, permissions, company settings,
        # and the ADMIN-only keys in MANAGER_EXCLUDED_PERMISSIONS (payment
        # reversal + brownfield adoption — mirrors the migration grant-copy).
        manager_permissions = connection.execute(
            text(
                "SELECT id FROM permission WHERE resource NOT IN ('roles', 'permissions', 'company') "
                f"AND name NOT IN ({_MANAGER_EXCLUDED_SQL})"
            )
        ).fetchall()

        for perm_row in manager_permissions:
            connection.execute(
                text(
                    "INSERT INTO role_permission (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id)"
                ),
                {
                    'role_id': role_ids['MANAGER'],
                    'permission_id': perm_row[0]
                }
            )

        logger.info(f"✓ MANAGER role assigned {len(manager_permissions)} permissions")

        # SALES: CRUD on clients, orders, recurring orders, read on products and dashboard
        sales_permission_names = [
            'clients.create', 'clients.read', 'clients.update',
            'orders.create', 'orders.read', 'orders.update',
            'payments.record', 'payments.read',
            'recurring_orders.create', 'recurring_orders.read', 'recurring_orders.update',
            'products.read',
            'dashboard.read',
            'tasks.create', 'tasks.read', 'tasks.update', 'tasks.delete',
            'task_states.read',
        ]

        sales_count = 0
        for perm_name in sales_permission_names:
            perm_row = connection.execute(
                text("SELECT id FROM permission WHERE name = :name"),
                {'name': perm_name}
            ).fetchone()

            if perm_row:
                connection.execute(
                    text(
                        "INSERT INTO role_permission (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id)"
                    ),
                    {
                        'role_id': role_ids['SALES'],
                        'permission_id': perm_row[0]
                    }
                )
                sales_count += 1

        logger.info(f"✓ SALES role assigned {sales_count} permissions")

        # USER: Read-only permissions
        user_permission_names = [
            'clients.read', 'orders.read', 'payments.read', 'products.read', 'recurring_orders.read', 'dashboard.read',
            'tasks.read', 'task_states.read',
        ]

        user_count = 0
        for perm_name in user_permission_names:
            perm_row = connection.execute(
                text("SELECT id FROM permission WHERE name = :name"),
                {'name': perm_name}
            ).fetchone()

            if perm_row:
                connection.execute(
                    text(
                        "INSERT INTO role_permission (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id)"
                    ),
                    {
                        'role_id': role_ids['USER'],
                        'permission_id': perm_row[0]
                    }
                )
                user_count += 1

        logger.info(f"✓ USER role assigned {user_count} permissions")

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

    # 3. Global system MANAGER holds everything except role/permission/company
    #    administration and the ADMIN-only MANAGER_EXCLUDED_PERMISSIONS keys.
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r CROSS JOIN permission p "
            "WHERE r.name = 'MANAGER' AND r.company_id IS NULL "
            "AND p.resource NOT IN ('roles', 'permissions', 'company') "
            f"AND p.name NOT IN ({_MANAGER_EXCLUDED_SQL}) "
            "ON CONFLICT DO NOTHING"
        )
    )

    # 4. Ledger grants derived from existing order grants (mirrors migration
    #    c1b's copy rule, but convergent: roles created AFTER c1b — e.g. by
    #    isp_seed — pick these up on the next migrate instead of never).
    #
    #    Cycle 2 (doc 18 amendment 7 / entity-merge verifier fix): the same
    #    mechanism protects every prod role holding a legacy products.*/
    #    recurring_orders.* grant from losing UI/API access once the
    #    products/recurring-orders pages retire in favor of the merged
    #    service_plans/client_services pages — every role that could act on
    #    the legacy resource keeps the equivalent ability on its successor.
    #    recurring_orders.generate has no legacy permission row today (the
    #    join below is then simply a no-op) — kept so a future backend-erp
    #    revision that adds it converges automatically with zero further
    #    models-utils changes.
    #
    #    client_services.adopt deliberately has NO legacy source (doc 30):
    #    adoption is a new ADMIN-only capability, never inherited from
    #    recurring_orders grants.
    for source, target in (
        ("orders.update", "payments.record"),
        ("orders.read", "payments.read"),
        ("orders.revert_payment", "payments.refund"),
        ("products.create", "service_plans.create"),
        ("products.read", "service_plans.read"),
        ("products.update", "service_plans.update"),
        ("products.delete", "service_plans.delete"),
        ("recurring_orders.create", "client_services.create"),
        ("recurring_orders.read", "client_services.read"),
        ("recurring_orders.update", "client_services.update"),
        # Closest legacy equivalents: RecurringOrder had no dedicated
        # suspend/reactivate permissions, so update is the source for both.
        ("recurring_orders.update", "client_services.suspend"),
        ("recurring_orders.update", "client_services.reactivate"),
        ("recurring_orders.delete", "client_services.delete"),
        ("recurring_orders.generate", "client_services.generate"),
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
