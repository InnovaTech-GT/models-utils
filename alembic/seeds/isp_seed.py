"""
ISP module seed: permissions, base roles, tier modules and device categories.

ld1_legacy_drop: the installable workflow-template catalog (workflow_template
table, WORKFLOW_TEMPLATES, _seed_workflow_templates) was removed.

Idempotent — every insert is ON CONFLICT DO NOTHING / DO UPDATE (workflow
templates upsert so blueprint revisions propagate) or existence-checked, so it
is safe on both fresh databases (called after rbac_seed) and existing tenants
(called from the isp-platform Alembic revision).

Cycle 2 (doc 18-cycle2-design.md, D5/D6): the free-form network graph
(network_node/network_node_type/network_link) is removed by revision
c2d_graph_removal. This module no longer seeds node types (deleted along with
DEFAULT_NODE_TYPES/_seed_default_node_types) and no longer grants network_*
permissions (deleted from ISP_PERMISSIONS/ISP_ROLES) — topologies.* replaces
them (D5's device-type-chain model). ISP_TIER_MODULES key 'network' is
replaced by 'topologies' (amendment 14); the one-time rewrite of ALREADY
SEEDED tier.modules rows ships in revision c2d_graph_removal itself (this
seed is append-only going forward and cannot rewrite existing JSON values).

Cycle 3 (doc 20-cycle3-design.md, E1/E2/E4): 'installation-provisioning' is
revised to v2 and 'suspension'/'reactivation'/'service-removal' to v4 —
all four now resolve their playbooks by walking the service's network path
(ENQUEUE_PROVISIONING use_service_path) instead of an explicit *_playbook_id
param (revision c3a_topology_purpose). `_seed_device_categories` (E4,
revision c3b_device_categories) is INSERT-ONLY convergent (ON CONFLICT key DO
NOTHING) — never DO UPDATE, so super-admin edits to the 13 baseline rows
survive every re-seed.
"""
import json
import logging
import os
import sys

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from database_utils.utils.timezone_utils import now_gt
# Cycle 3 E4: env.py imports every model module before calling seed_isp_data,
# so this top-level import is safe (no circular import — models never import
# seeds). Used to derive TEMPLATE_REQUIRED_COLUMNS from the model instead of
# a hand-typed string literal (doc 20a workflow-provisioning verifier fix —
# a typo'd table/column name silently deactivates the gated templates
# forever via the retirement pass, with no test catching it at head).

logger = logging.getLogger(__name__)


ISP_PERMISSIONS = [
    # Service plans
    {"name": "service_plans.create", "resource": "service_plans", "action": "create", "description": "Create service plans"},
    {"name": "service_plans.read", "resource": "service_plans", "action": "read", "description": "View service plans"},
    {"name": "service_plans.update", "resource": "service_plans", "action": "update", "description": "Update service plans"},
    {"name": "service_plans.delete", "resource": "service_plans", "action": "delete", "description": "Delete service plans"},
    # Client services (subscriptions)
    {"name": "client_services.create", "resource": "client_services", "action": "create", "description": "Create subscriber services"},
    {"name": "client_services.read", "resource": "client_services", "action": "read", "description": "View subscriber services"},
    {"name": "client_services.update", "resource": "client_services", "action": "update", "description": "Update subscriber services"},
    {"name": "client_services.delete", "resource": "client_services", "action": "delete", "description": "Delete subscriber services"},
    {"name": "client_services.suspend", "resource": "client_services", "action": "suspend", "description": "Suspend a subscriber service"},
    {"name": "client_services.reactivate", "resource": "client_services", "action": "reactivate", "description": "Reactivate a suspended service"},
    # Cycle 2 D1: manual single-cycle billing generation (client_services
    # absorbs recurring_order's billing engine).
    {"name": "client_services.generate", "resource": "client_services", "action": "generate", "description": "Manually generate a billing cycle for a service"},
    # Brownfield adoption (doc 30, revision ba1): attest a pre-existing
    # service as installed. ADMIN-only — no ISP_ROLES entry grants it.
    # (payments.refund precedent in rbac_seed). Deliberately granted to NO
    # ISP base role and given NO legacy grant-copy source in rbac_seed step 4.
    {"name": "client_services.adopt", "resource": "client_services", "action": "adopt", "description": "Attest a service as installed (brownfield adoption) - ADMIN only"},
    # Inventory
    {"name": "device_types.create", "resource": "device_types", "action": "create", "description": "Create device catalog entries"},
    {"name": "device_types.read", "resource": "device_types", "action": "read", "description": "View device catalog"},
    {"name": "device_types.update", "resource": "device_types", "action": "update", "description": "Update device catalog entries"},
    {"name": "device_types.delete", "resource": "device_types", "action": "delete", "description": "Delete device catalog entries"},
    {"name": "warehouses.create", "resource": "warehouses", "action": "create", "description": "Create warehouses"},
    {"name": "warehouses.read", "resource": "warehouses", "action": "read", "description": "View warehouses"},
    {"name": "warehouses.update", "resource": "warehouses", "action": "update", "description": "Update warehouses"},
    {"name": "warehouses.delete", "resource": "warehouses", "action": "delete", "description": "Delete warehouses"},
    {"name": "inventory_items.create", "resource": "inventory_items", "action": "create", "description": "Register inventory items"},
    {"name": "inventory_items.read", "resource": "inventory_items", "action": "read", "description": "View inventory items"},
    {"name": "inventory_items.update", "resource": "inventory_items", "action": "update", "description": "Update inventory items"},
    {"name": "inventory_items.delete", "resource": "inventory_items", "action": "delete", "description": "Delete inventory items"},
    {"name": "equipment_events.create", "resource": "equipment_events", "action": "create", "description": "Record equipment lifecycle events"},
    {"name": "equipment_events.read", "resource": "equipment_events", "action": "read", "description": "View equipment history"},
    # Topology (Cycle 2 D5) — replaces network_node_types/network_nodes/
    # network_links (removed; revision c2d_graph_removal deletes the tables
    # and the role_permission/permission rows naming them).
    {"name": "topologies.create", "resource": "topologies", "action": "create", "description": "Create provisioning topologies"},
    {"name": "topologies.read", "resource": "topologies", "action": "read", "description": "View provisioning topologies"},
    {"name": "topologies.update", "resource": "topologies", "action": "update", "description": "Update provisioning topologies"},
    {"name": "topologies.delete", "resource": "topologies", "action": "delete", "description": "Delete provisioning topologies"},
    # Provisioning automation
    {"name": "playbooks.create", "resource": "playbooks", "action": "create", "description": "Upload provisioning playbooks"},
    {"name": "playbooks.read", "resource": "playbooks", "action": "read", "description": "View provisioning playbooks"},
    {"name": "playbooks.update", "resource": "playbooks", "action": "update", "description": "Update provisioning playbooks"},
    {"name": "playbooks.delete", "resource": "playbooks", "action": "delete", "description": "Delete provisioning playbooks"},
    {"name": "provisioning.read", "resource": "provisioning", "action": "read", "description": "View provisioning jobs"},
    {"name": "provisioning.create", "resource": "provisioning", "action": "create", "description": "Enqueue provisioning jobs"},
    {"name": "provisioning.execute", "resource": "provisioning", "action": "execute", "description": "Execute/retry provisioning jobs"},
    {"name": "provisioning.cancel", "resource": "provisioning", "action": "cancel", "description": "Cancel provisioning jobs"},
    # Insights (Cycle 4) — available to every tenant, no tier module gate.
    {"name": "insights.create", "resource": "insights", "action": "create", "description": "Create insight dashboards/charts"},
    {"name": "insights.read", "resource": "insights", "action": "read", "description": "View insight dashboards"},
    {"name": "insights.update", "resource": "insights", "action": "update", "description": "Update insight dashboards/charts"},
    {"name": "insights.delete", "resource": "insights", "action": "delete", "description": "Delete insight dashboards/charts"},
    # Network configuration (Cycle 5 Phase 1: TR-069 / GenieACS) — INSERT-ONLY
    # convergent (ON CONFLICT DO NOTHING); ADMIN auto-inherits all via
    # _seed_permissions.
    {"name": "network_access.read", "resource": "network_access", "action": "read", "description": "View network transport paths"},
    {"name": "network_access.create", "resource": "network_access", "action": "create", "description": "Create network transport paths"},
    {"name": "network_access.update", "resource": "network_access", "action": "update", "description": "Update network transport paths"},
    {"name": "network_access.delete", "resource": "network_access", "action": "delete", "description": "Delete network transport paths"},
    {"name": "device_credentials.read", "resource": "device_credentials", "action": "read", "description": "View device credentials (secrets never exposed)"},
    {"name": "device_credentials.create", "resource": "device_credentials", "action": "create", "description": "Create device credentials"},
    {"name": "device_credentials.update", "resource": "device_credentials", "action": "update", "description": "Update/rotate device credentials"},
    {"name": "device_credentials.delete", "resource": "device_credentials", "action": "delete", "description": "Delete device credentials"},
    # ac1 (Capa 3): a deliberate, audited exception to the write-only-secrets
    # canon — the tenant CWMP Inform password must be readable back because an
    # installer types it into the router by hand. ADMIN role ONLY (see
    # no ISP_ROLES entry grants it); no other base role is granted it.
    {"name": "device_credentials.reveal", "resource": "device_credentials", "action": "reveal", "description": "Reveal a device credential's plaintext secret (audited)"},
    {"name": "acs_devices.read", "resource": "acs_devices", "action": "read", "description": "View ACS/TR-069 device state"},
    {"name": "acs_devices.action", "resource": "acs_devices", "action": "action", "description": "Run ACS device actions (reboot, factory-reset, refresh)"},
    {"name": "provisioning_settings.read", "resource": "provisioning_settings", "action": "read", "description": "View tenant provisioning settings/enable gate"},
    {"name": "provisioning_settings.update", "resource": "provisioning_settings", "action": "update", "description": "Update tenant provisioning settings/enable gate"},
    {"name": "acs_registrations.read", "resource": "acs_registrations", "action": "read", "description": "View ACS device registrations"},
    {"name": "acs_registrations.create", "resource": "acs_registrations", "action": "create", "description": "Create ACS device registrations (incl. bulk import)"},
    {"name": "acs_registrations.update", "resource": "acs_registrations", "action": "update", "description": "Update ACS device registrations"},
    {"name": "acs_registrations.delete", "resource": "acs_registrations", "action": "delete", "description": "Release/delete ACS device registrations"},
    {"name": "network_audit.read", "resource": "network_audit", "action": "read", "description": "View the append-only device action log"},
]

# New ISP base roles (global: company_id NULL) and their permission grants.
ISP_ROLES = {
    "TECHNICIAN": {
        "description": "Field technician: installations, equipment handling, dispatch board",
        "permissions": [
            "tasks.create", "tasks.read", "tasks.update",
            "clients.read", "clients.update",
            "client_services.read", "client_services.update",
            "inventory_items.read", "inventory_items.update",
            "equipment_events.create", "equipment_events.read",
            "warehouses.read", "device_types.read",
            # Cycle 2 D5: replaces network_nodes.read/network_node_types.read/
            # network_links.read (topology read is the analogous grant).
            "topologies.read",
            "provisioning.read",
            "dashboard.read",
            # Cycle 4: insights are available to every tenant/role that has
            # the dashboard — read-only for base roles, full CRUD is
            # ADMIN-only (granted automatically in _seed_permissions).
            "insights.read",
            # Cycle 5 Phase 1: field techs register CPEs and read ACS device state.
            "acs_registrations.create", "acs_registrations.read",
            "acs_devices.read",
            # cfg3: the technician mobile app. Row declared in
            # rbac_seed.PERMISSIONS_DATA, granted here.
            "mobile.technician",
            # mp1_technician_plan_read: plan picker of the install-order sheet.
            "service_plans.read",
        ],
    },
    # tk2 (Figma redesign PR 4, master plan §2.7): the cobrador. Distinct from
    # a billing clerk — a collector walks a route with cash, so the grant list is the
    # minimum that lets the mobile app show "who owes what" and record the
    # payment: NO order/plan creation, NO client edits.
    "COLLECTOR": {
        "description": "Field collector: collection routes, cash sessions, payment recording",
        "permissions": [
            "tasks.read",
            "clients.read",
            "payments.read", "payments.record",
            "orders.read",
            "client_services.read",
            # mi2_mobile_field_ops (uplink-mobile cobros): the app gate, the
            # create-order sheet + field-staff picker, plan names in pickers,
            # and the MUFA picker / serial lookup. Pinned against mi2's
            # COLLECTOR_GRANTS by tests/test_mobile_rbac_seed.py.
            "mobile.collector",
            "tasks.create",
            "service_plans.read",
            "inventory_items.read",
        ],
    },
}

# Cycle 2 amendment 14: 'network' -> 'topologies' (D5). The key 'network'
# stays inert forever in ALREADY-SEEDED tier.modules JSON rows (the one-time
# rewrite of those rows is a data migration, not this append-only seed) — see
# revision c2d_graph_removal step 10.
ISP_TIER_MODULES = ["inventory", "topologies", "provisioning"]


# Cycle 3 E4 (doc 20a admin-categories-sidebar §6): the 13 baseline device
# categories, duplicated (not imported) from revision
# c3b_device_categories_global_table.py's CATEGORIES literal — revisions are
# immutable forever; this list may grow independently in later cycles
# without a new migration (a future baseline category is added here only).
# Cycle 7 (doc 25 §2.1, revision nc2a_core_config): 4th element = CORE/EDGE
# tier (None = passives/unclassified); ONU's display name becomes
# 'ONU / ONT' (key immutable — nc2a updates existing rows still named 'ONU',
# this seed only affects fresh inserts). Tier converges for EXISTING rows via
# the gated backfill in _seed_device_categories (fires only while NO row has
# a tier yet — the pre-nc2a data signature) — never a DO UPDATE and never an
# every-run UPDATE, so super-admin tier/name edits (including clearing a tier
# back to NULL) survive every re-seed.
# Figma redesign PR 8 (docs/design/plans/08-inventario.md §2.1, revision
# inv1_general_inventory): 6th element = the lucide icon name, and 9 new system
# categories that make this an INVENTORY list rather than a network-gear list
# (consumables, tools, SIM cards) plus MUFA. MUFA is a NEW key rather than a
# rename of SPLICE_CLOSURE: `key` is immutable, PR 9 needs the Figma-level name,
# and both stay passive/NULL-tier.
# (key, display name, sort order, tier, is_passive, icon, is_active)
#
# is_active (2026-09-17, revision dc1_category_trim): the USER DECISION global
# trim to the six product-backed categories (ROUTER, SWITCH, OLT, ONU,
# FIBER_OPTIC, PATCH_CORD) that back backend-erp's default-products-per-tenant
# seeding (utils/inventory_defaults.py). All 22 keys stay in this list — key
# is immutable and RESTRICT FKs from device_type reference these rows — only
# is_active flips. dc1_category_trim converges EXISTING rows; this column
# only governs what a genuinely fresh insert gets.
#
# is_passive (Cycle 10, doc 35 §2.3) marks SIGNAL-passive gear: it appears on a
# service's configuration path and matters for troubleshooting, but nothing is
# ever configured on it. It is NOT "has no playbook" — that distinction is the
# whole point. Without the flag, an OLT whose ACTIVATION playbook someone
# forgot to bind looks exactly like a splitter.
#
# UPS and RADIO are deliberately NOT passive: a UPS may well expose SNMP, and a
# radio is an active link end. Marking them passive would silently exclude them
# from provisioning forever.
# The nine rows inv1_general_inventory owns, and the tier vocabulary that
# predates it. Before inv1 the ck_device_category_tier CHECK only allows
# CORE/EDGE/NULL, and this seed also runs at those older migration positions
# (stepped upgrades, and after a downgrade) — so pre-inv1 the new rows are
# skipped outright and a new-vocabulary tier is seeded as NULL. inv1 inserts
# them properly on the way back up.
_INV1_CATEGORY_KEYS = {
    'MUFA', 'PATCH_CORD', 'FIBER_OPTIC', 'DISTRIBUTION_BOX', 'MODEM',
    'SIM_CARD', 'SET_TOP_BOX', 'FUSION_SPLICER', 'BARCODE_SCANNER',
}
_PRE_INV1_TIERS = (None, 'CORE', 'EDGE')

# ROUTER/OLT icons are 'router'/'server' since ci1_category_icons (was radio-tower/radio).
DEVICE_CATEGORIES = [
    ('ROUTER', 'Router', 10, 'CORE', False, 'router', True),
    ('SWITCH', 'Switch', 20, 'CORE', False, 'network', True),
    ('OLT', 'OLT', 30, 'CORE', False, 'server', True),
    ('ONU', 'ONU / ONT', 40, 'EDGE', False, 'house-wifi', True),
    ('SPLITTER', 'Splitter', 50, None, True, 'split', False),
    ('SPLICE_CLOSURE', 'Splice Closure', 60, None, True, 'box', False),
    ('MUFA', 'MUFA', 65, None, True, 'box', False),
    ('PATCH_PANEL', 'Patch Panel', 70, None, True, 'gallery-thumbnails', False),
    ('ACCESS_POINT', 'Access Point', 80, 'EDGE', False, 'radio-tower', False),
    ('CPE_ROUTER', 'CPE Router', 90, 'EDGE', False, 'house-wifi', False),
    ('UPS', 'UPS', 100, None, False, 'activity', False),
    ('ANTENNA', 'Antenna', 110, None, True, 'antenna', False),
    ('RADIO', 'Radio', 120, None, False, 'radio', False),
    ('OTHER', 'Other', 130, 'OTHER', False, 'box', False),
    ('PATCH_CORD', 'Patch cords', 200, 'CONSUMABLE', False, 'cable', True),
    ('FIBER_OPTIC', 'Fibra optica', 210, 'CONSUMABLE', False, 'cable', True),
    ('DISTRIBUTION_BOX', 'Cajas de distribucion', 220, 'CONSUMABLE', False, 'box', False),
    ('MODEM', 'Modems', 230, 'EDGE', False, 'activity', False),
    ('SIM_CARD', 'Tarjetas SIM', 240, 'OTHER', False, 'smartphone', False),
    ('SET_TOP_BOX', 'Decodificadores', 250, 'EDGE', False, 'gallery-thumbnails', False),
    ('FUSION_SPLICER', 'Fusionadora de fibra', 300, 'TOOL', False, 'wrench', False),
    ('BARCODE_SCANNER', 'Escaner de codigos', 310, 'TOOL', False, 'scan-qr-code', False),
]


def seed_isp_data(connection: Connection) -> None:
    """Seed ISP permissions, roles, tier modules and device categories."""
    _seed_permissions(connection)
    _seed_roles(connection)
    _seed_tier_modules(connection)
    _seed_device_categories(connection)
    logger.info("ISP seed completed")


def _seed_permissions(connection: Connection) -> None:
    for perm in ISP_PERMISSIONS:
        connection.execute(
            text(
                "INSERT INTO permission (id, created_at, name, resource, action, description) "
                "VALUES (gen_random_uuid(), :created_at, :name, :resource, :action, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"created_at": now_gt(), **perm},
        )
    # ADMIN inherits all new permissions (matching rbac_seed policy).
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r, permission p "
            "WHERE r.name = 'ADMIN' AND r.company_id IS NULL AND p.name = ANY(:names) "
            "ON CONFLICT DO NOTHING"
        ),
        {"names": [p["name"] for p in ISP_PERMISSIONS]},
    )
    logger.info(f"Seeded {len(ISP_PERMISSIONS)} ISP permissions")


def _seed_roles(connection: Connection) -> None:
    for role_name, spec in ISP_ROLES.items():
        row = connection.execute(
            text("SELECT id FROM role WHERE name = :name AND company_id IS NULL"),
            {"name": role_name},
        ).fetchone()
        if row:
            role_id = row[0]
        else:
            role_id = connection.execute(
                text(
                    "INSERT INTO role (id, created_at, name, description, is_system) "
                    "VALUES (gen_random_uuid(), :created_at, :name, :description, TRUE) "
                    "RETURNING id"
                ),
                {"created_at": now_gt(), "name": role_name, "description": spec["description"]},
            ).fetchone()[0]
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT :role_id, p.id FROM permission p WHERE p.name = ANY(:names) "
                "ON CONFLICT DO NOTHING"
            ),
            {"role_id": role_id, "names": spec["permissions"]},
        )
    logger.info(f"Seeded {len(ISP_ROLES)} ISP roles")


def _seed_tier_modules(connection: Connection) -> None:
    """Append the ISP module keys to every tier's modules JSON list."""
    rows = connection.execute(text("SELECT id, modules FROM tier")).fetchall()
    for tier_id, modules in rows:
        current = modules or []
        if isinstance(current, str):
            current = json.loads(current)
        merged = list(dict.fromkeys([*current, *ISP_TIER_MODULES]))
        if merged != current:
            connection.execute(
                text("UPDATE tier SET modules = :modules WHERE id = :id"),
                {"modules": json.dumps(merged), "id": tier_id},
            )
    logger.info("Tier modules updated with ISP modules")


def _seed_device_categories(connection: Connection) -> None:
    """Cycle 3 E4 (doc 20a admin-categories-sidebar §6): INSERT-ONLY,
    ON CONFLICT (key) DO NOTHING — NEVER DO UPDATE. Super-admins own
    name/sort_order/icon/is_active for these rows once created (auth-erp
    admin_device_categories.py); a convergent DO UPDATE seed would silently
    revert their edits on every migrate. This seed guarantees exactly one
    thing forever: the baseline keys EXIST — a super-admin cannot
    permanently delete a system key (blocked at the router anyway), but can
    deactivate it, and that survives every re-seed.

    Table-existence-guarded (network_node_type precedent noted in env.py)
    so a pre-c3b DB at this migration position skips cleanly instead of
    erroring."""
    exists = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'device_category'"
    )).scalar()
    if not exists:
        logger.warning(
            "Skipping device_category seed: table does not exist at this migration position"
        )
        return

    # Cycle 7 (doc 25 §2.1): column-existence gate, same migration-position
    # logic as the table gate above — this seed also runs on pre-nc2a
    # positions (stepped/partial upgrades) where device_category.tier does
    # not exist yet.
    has_tier = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name = 'device_category' AND column_name = 'tier'"
    )).scalar()

    # Migration-position gate for the inv1 vocabulary: quantity is inv1's own
    # column, so its presence means the widened CHECK is in place.
    has_general_inventory = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name = 'inventory_item' AND column_name = 'quantity'"
    )).scalar()

    for key, name, sort_order, tier, _is_passive, icon, is_active in DEVICE_CATEGORIES:
        if not has_general_inventory:
            if key in _INV1_CATEGORY_KEYS:
                continue
            if tier not in _PRE_INV1_TIERS:
                tier = None
        if has_tier:
            connection.execute(
                text(
                    "INSERT INTO device_category (id, key, name, sort_order, tier, icon, is_active, is_system, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :key, :name, :sort_order, :tier, :icon, :is_active, TRUE, :created_at, :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": key, "name": name, "sort_order": sort_order, "tier": tier,
                 "icon": icon, "is_active": is_active, "created_at": now_gt()},
            )
        else:
            # Pre-nc2a position: no tier column. `icon` has existed since c3b.
            connection.execute(
                text(
                    "INSERT INTO device_category (id, key, name, sort_order, icon, is_active, is_system, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :key, :name, :sort_order, :icon, :is_active, TRUE, :created_at, :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": key, "name": name, "sort_order": sort_order, "icon": icon,
                 "is_active": is_active, "created_at": now_gt()},
            )

    # Cycle 7 tier convergence for rows that predate nc2a: the nc2a backfill
    # only runs at migration time, so a pre-nc2a prod dump loaded into an
    # already-migrated schema (./scripts/load-prod-data.sh) leaves every tier
    # NULL. Backfill ONLY in that state — no row classified anywhere — because
    # a bare per-row tier-IS-NULL UPDATE cannot tell "never classified" apart
    # from an admin clearing a tier back to NULL (DeviceCategoryUpdate
    # explicitly supports clear-to-NULL for passives): once any tier is set,
    # the seed never touches the column again and admin edits survive every
    # re-seed (the insert-only DO NOTHING covenant above, extended to one
    # column).
    if has_tier:
        any_classified = connection.execute(text(
            "SELECT COUNT(*) FROM device_category WHERE tier IS NOT NULL"
        )).scalar()
        if not any_classified:
            for key, _name, _sort_order, tier, _passive, _icon, _active in DEVICE_CATEGORIES:
                if tier is None:
                    continue
                if not has_general_inventory and tier not in _PRE_INV1_TIERS:
                    continue
                connection.execute(
                    text("UPDATE device_category SET tier = :tier WHERE key = :key AND tier IS NULL"),
                    {"tier": tier, "key": key},
                )
    # Cycle 10 passive convergence, following the tier precedent above EXACTLY:
    # classify only while NOTHING anywhere is classified. A per-row
    # is_passive-is-false UPDATE cannot tell "never classified" apart from a
    # super-admin who deliberately marked a splitter active (a tenant with
    # managed splitters that report optical power would do precisely that), and
    # reverting that decision on every migrate is the bug the tier block was
    # written to avoid.
    has_passive = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        " WHERE table_name = 'device_category' AND column_name = 'is_passive'"
    )).scalar()
    if has_passive:
        any_passive = connection.execute(text(
            "SELECT COUNT(*) FROM device_category WHERE is_passive"
        )).scalar()
        if not any_passive:
            for key, _name, _sort_order, _tier, is_passive, _icon, _active in DEVICE_CATEGORIES:
                if not is_passive:
                    continue
                connection.execute(
                    text("UPDATE device_category SET is_passive = true WHERE key = :key"),
                    {"key": key},
                )
    else:
        logger.warning(
            "device_category.is_passive missing (pre-ng1 position) — skipping the "
            "passive classification"
        )

    logger.info(f"Seeded {len(DEVICE_CATEGORIES)} baseline device categories (insert-only, convergent)")
