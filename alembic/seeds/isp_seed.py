"""
ISP module seed: permissions, base roles, tier modules, and installable
workflow templates (ADR-007/008).

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
from database_utils.models.isp import DeviceTypePlaybook
from database_utils.utils.workflow_engine import KNOWN_RESOURCE_TYPES

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
    # service as installed. ADMIN-only — listed in ADMIN_ONLY_PERMISSIONS
    # below so the MANAGER auto-inherit in _seed_permissions skips it
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
    # Workflow templates
    {"name": "workflow_templates.read", "resource": "workflow_templates", "action": "read", "description": "Browse workflow template catalog"},
    {"name": "workflow_templates.install", "resource": "workflow_templates", "action": "install", "description": "Install a workflow template"},
    # Insights (Cycle 4) — available to every tenant, no tier module gate.
    {"name": "insights.create", "resource": "insights", "action": "create", "description": "Create insight dashboards/charts"},
    {"name": "insights.read", "resource": "insights", "action": "read", "description": "View insight dashboards"},
    {"name": "insights.update", "resource": "insights", "action": "update", "description": "Update insight dashboards/charts"},
    {"name": "insights.delete", "resource": "insights", "action": "delete", "description": "Delete insight dashboards/charts"},
    # Network configuration (Cycle 5 Phase 1: TR-069 / GenieACS) — INSERT-ONLY
    # convergent (ON CONFLICT DO NOTHING); ADMIN/MANAGER auto-inherit all via
    # _seed_permissions.
    {"name": "network_access.read", "resource": "network_access", "action": "read", "description": "View network transport paths"},
    {"name": "network_access.create", "resource": "network_access", "action": "create", "description": "Create network transport paths"},
    {"name": "network_access.update", "resource": "network_access", "action": "update", "description": "Update network transport paths"},
    {"name": "network_access.delete", "resource": "network_access", "action": "delete", "description": "Delete network transport paths"},
    {"name": "device_credentials.read", "resource": "device_credentials", "action": "read", "description": "View device credentials (secrets never exposed)"},
    {"name": "device_credentials.create", "resource": "device_credentials", "action": "create", "description": "Create device credentials"},
    {"name": "device_credentials.update", "resource": "device_credentials", "action": "update", "description": "Update/rotate device credentials"},
    {"name": "device_credentials.delete", "resource": "device_credentials", "action": "delete", "description": "Delete device credentials"},
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

# ADMIN-only permission names: excluded from the MANAGER auto-inherit below.
# MUST stay a subset of rbac_seed.MANAGER_EXCLUDED_PERMISSIONS (the two
# copies are pinned equal-by-membership in tests/test_attested_adoption.py —
# seeds cannot import each other: tests load them by file path, nc2a
# duplicated-fragment precedent).
ADMIN_ONLY_PERMISSIONS = ("client_services.adopt",)

# New ISP base roles (global: company_id NULL) and their permission grants.
ISP_ROLES = {
    "TECHNICIAN": {
        "description": "Field technician: installations, equipment handling, dispatch board",
        "permissions": [
            "tasks.create", "tasks.read", "tasks.update",
            "task_states.read",
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
            # ADMIN/MANAGER-only (granted automatically in _seed_permissions).
            "insights.read",
            # Cycle 5 Phase 1: field techs register CPEs and read ACS device state.
            "acs_registrations.create", "acs_registrations.read",
            "acs_devices.read",
        ],
    },
    "NOC": {
        "description": "Network operations: topology, playbooks, provisioning jobs",
        "permissions": [
            # Cycle 2 D5: replaces network_node_types.*/network_nodes.*/
            # network_links.* (full CRUD, same operational role).
            "topologies.create", "topologies.read", "topologies.update", "topologies.delete",
            "playbooks.create", "playbooks.read", "playbooks.update", "playbooks.delete",
            "provisioning.read", "provisioning.create", "provisioning.execute", "provisioning.cancel",
            "client_services.read",
            "inventory_items.read", "device_types.read",
            "workflow_templates.read",
            "tasks.create", "tasks.read", "tasks.update",
            "task_states.read",
            "dashboard.read",
            "insights.read",
            # Cycle 5 Phase 1: NOC owns the network-configuration surface.
            "network_access.read", "network_access.create", "network_access.update", "network_access.delete",
            "device_credentials.read", "device_credentials.create", "device_credentials.update", "device_credentials.delete",
            "acs_devices.read", "acs_devices.action",
            "acs_registrations.read", "acs_registrations.create", "acs_registrations.update", "acs_registrations.delete",
            "provisioning_settings.read", "provisioning_settings.update",
            "network_audit.read",
        ],
    },
    "WAREHOUSE": {
        "description": "Warehouse custody: device catalog, stock, transfers",
        "permissions": [
            "device_types.create", "device_types.read", "device_types.update", "device_types.delete",
            "warehouses.create", "warehouses.read", "warehouses.update", "warehouses.delete",
            "inventory_items.create", "inventory_items.read", "inventory_items.update", "inventory_items.delete",
            "equipment_events.create", "equipment_events.read",
            "dashboard.read",
            "insights.read",
        ],
    },
    "SUPPORT": {
        "description": "Subscriber care: client data, services, suspensions, tickets",
        "permissions": [
            "clients.create", "clients.read", "clients.update",
            "client_services.create", "client_services.read", "client_services.update",
            "client_services.suspend", "client_services.reactivate",
            "service_plans.read",
            "orders.read", "recurring_orders.read",
            "tasks.create", "tasks.read", "tasks.update",
            "task_states.read",
            "provisioning.read",
            "dashboard.read",
            "insights.read",
            # Cycle 5 Phase 1: subscriber-care read visibility into ACS state.
            "acs_devices.read", "acs_registrations.read",
            "network_audit.read", "provisioning_settings.read",
        ],
    },
    "BILLING": {
        "description": "Billing operations: orders, recurring billing, plans",
        "permissions": [
            "clients.read",
            "orders.create", "orders.read", "orders.update",
            "recurring_orders.create", "recurring_orders.read", "recurring_orders.update",
            "products.read",
            "service_plans.read", "client_services.read",
            # Cycle 2 D1: BILLING owns the manual billing-cycle generation
            # action that replaces recurring_orders' equivalent.
            "client_services.generate",
            "dashboard.read",
            "insights.read",
        ],
    },
}

# Cycle 2 amendment 14: 'network' -> 'topologies' (D5). The key 'network'
# stays inert forever in ALREADY-SEEDED tier.modules JSON rows (the one-time
# rewrite of those rows is a data migration, not this append-only seed) — see
# revision c2d_graph_removal step 10.
ISP_TIER_MODULES = ["inventory", "topologies", "provisioning"]


def _wt(key, name, description, category, parameters, triggers, steps, edges):
    return {
        "key": key, "name": name, "description": description, "category": category,
        "definition": {
            "parameters": parameters, "triggers": triggers, "steps": steps, "edges": edges,
        },
    }


# Per-template column dependencies (doc 18 amendment 10 / topology-networking
# verifier fix): the seed runs after EVERY alembic command, including stepped
# partial upgrades, so a template whose steps reference a column from a LATER
# revision must be skipped until that revision has actually applied — the
# stepactiontype enum gate below already does this for step action types;
# this dict extends the same idea to plain columns. Checked against
# information_schema.columns in _seed_workflow_templates.
TEMPLATE_REQUIRED_COLUMNS = {
    # Cycle 10 (doc 35 §2.4): 'installation-provisioning' uses
    # ENQUEUE_PROVISIONING's use_service_path mode, which resolves each device
    # on the traversed path through the device_type_playbook binding table
    # (revision ng1_network_graph) — never publish before it exists. Same gate
    # as before, pointed at the table that replaced topology_playbook.
    "installation-provisioning": [(DeviceTypePlaybook.__tablename__, "purpose")],
    # v3 (Cycle 2 §1b rewrite, doc 18 amendment 8): these templates now
    # UPDATE_FIELD client_service.billing_status instead of
    # recurring_order.status — the column only exists from c2b onward. v4
    # (Cycle 3 E2) adds the same topology_playbook.purpose gate as above.
    # 'service-removal', 'suspension' and 'reactivation' had identical entries,
    # dropped with the templates themselves (service-lifecycle cycle) — this
    # dict is only consulted per entry in WORKFLOW_TEMPLATES, so a key with no
    # template is dead weight.
}

# Installable workflow templates (ADR-007). "{{param:KEY}}" placeholders are
# resolved at install time; "{{trigger.*}}" placeholders stay for runtime.
WORKFLOW_TEMPLATES = [
    # v3 (client-install-field removal, revision cf1): the old s3 UPDATE_FIELD
    # step (client.installation_status display cache) is GONE along with the
    # column itself — s2 is now terminal; cf1's data pass deletes s3 from
    # already-installed tenant copies. v2 history (doc 16 §5.4,
    # installation-flow): PENDING_INSTALL-gated trigger so imports/backfills
    # creating ACTIVE services never spawn install orders; s1 bills the
    # installation fee (CREATE_ORDER), s2 opens the dispatch task
    # (CREATE_TASK, linked CLIENT_SERVICE — the Cycle-3 join key; ORDER linkage
    # is forbidden). Free installs = a Q0 fee product; the order still exists
    # as history and is settled via settle-zero. Tenants must reinstall to
    # pick up a new version.
    # v4 (task-context cycle, doc 32): the installation-fee param is now a
    # SERVICE PLAN (type "service_plan", item key service_plan_id — the
    # engine's preferred resolution). The old product param blocked fresh
    # tenants entirely: legacy products are retired (no create path), so a
    # new company could never satisfy the required product UUID. Tenants on
    # v3 keep their installed product_id copies (deprecated-but-honored
    # during the rollback window).
    _wt(
        "new-installation", "New Installation",
        "When a subscriber service is created pending installation, bill the installation fee "
        "and open an installation task on the dispatch board.",
        "installation",
        [
            {"key": "install_state_id", "label": "Board column for new installations", "type": "task_state", "required": True},
            {"key": "installation_fee_plan_id", "label": "Installation fee catalog item (use a Q0 plan for free installs)", "type": "service_plan", "required": True},
            {"key": "fixed_assignee_ids", "label": "Fallback technicians when the client has no assigned technician", "type": "users", "required": False},
        ],
        [{"resource_type": "client_service", "event_type": "CREATED",
          "field_conditions": {"field": "status", "operator": "equals", "value": "PENDING_INSTALL"}}],
        [
            {"ref": "s1", "name": "Create installation order", "action_type": "CREATE_ORDER",
             "action_config": {
                 "order_type": "INSTALLATION",
                 "client_id": "{{trigger.after.client_id}}",
                 "client_service_id": "{{trigger.resource_id}}",
                 "items": [{"service_plan_id": "{{param:installation_fee_plan_id}}", "quantity": 1}],
                 "due_date_offset_days": 0,
                 "idempotency_key": "install-order-{{trigger.resource_id}}"}},
            {"ref": "s2", "name": "Create installation task", "action_type": "CREATE_TASK",
             "action_config": {
                 "name": "New installation — service {{trigger.resource_id}}",
                 "description": "Install subscriber service. Client: {{trigger.after.client_id}}. "
                                "Installation order: {{steps.s1.resource_id}}",
                 "task_state_id": "{{param:install_state_id}}",
                 "linked_object_type": "CLIENT_SERVICE",
                 "linked_object_id": "{{trigger.resource_id}}",
                 "assignee_source": "client_technician",
                 "assignee_ids": "{{param:fixed_assignee_ids}}",
                 "client_id": "{{trigger.after.client_id}}"}},
        ],
        [{"from": "s1", "to": "s2"}],
    ),
    # v2 (Cycle 3 E2, doc 20a workflow-provisioning §3): rewritten from an
    # explicit `activation_playbook_id` param to topology purpose resolution
    # (ENQUEUE_PROVISIONING use_service_path). The task's linked_object_id (set by
    # new-installation's s2 to the client_service that fired new-installation)
    # is the resolution target; `purpose` is fixed to ACTIVATION (not a
    # param — the founder flow is specifically install -> activate; purpose
    # flexibility lives in the workflow editor for hand-built automations).
    # Tenants with v1 installed keep running mode B (explicit playbook_id)
    # until they reinstall — mode B is unchanged and stays supported.
    _wt(
        "installation-provisioning", "Installation → Provisioning",
        "When an installation task is moved to the 'installed' column, resolve the "
        "linked service's network path and run each device's ACTIVATION playbook.",
        "installation",
        [
            {"key": "installed_state_id", "label": "Board column meaning 'installation done'",
             "type": "task_state", "required": True},
        ],
        [{"resource_type": "task", "event_type": "UPDATED",
          "field_conditions": {"field": "task_state_id", "operator": "changed_to",
                                "value": "{{param:installed_state_id}}"}}],
        [{"ref": "provision", "name": "Provision service from its network path",
          "action_type": "ENQUEUE_PROVISIONING",
          "action_config": {
              "use_service_path": True,
              "purpose": "ACTIVATION",
              "client_service_id": "{{trigger.after.linked_object_id}}",
              "idempotency_key": "activate-{{trigger.after.linked_object_id}}",
              "max_attempts": 3}}],
        [],
    ),
    _wt(
        "service-activation", "Service Activation on Provisioning Success",
        "When a provisioning job succeeds, mark the linked service ACTIVE (billing starts via the plan's recurring order).",
        "installation",
        [],
        [{"resource_type": "provisioning_job", "event_type": "UPDATED",
          "field_conditions": {"field": "status", "operator": "changed_to", "value": "SUCCEEDED"}}],
        [{"ref": "activate", "name": "Activate service", "action_type": "UPDATE_FIELD",
          "action_config": {"resource_type": "client_service", "resource_id_source": "custom",
                            "resource_id": "{{trigger.after.client_service_id}}",
                            "updates": {"status": "ACTIVE"}}}],
        [],
    ),
    # 'suspension' and 'reactivation' REMOVED (service-lifecycle cycle) — the
    # siblings of 'service-removal' above, retired for the same reason and by
    # the same mechanism (revision lc2_retire_susp_react, which also
    # deactivates already-installed tenant copies).
    #
    # Both halves of each template are now redundant:
    #   1. The UPDATE_FIELD billing step — _apply_suspension/_apply_reactivation
    #      in backend-erp set billing_status natively (PAUSED / ACTIVE, and
    #      reactivation stamps next_generation_date when NULL so a reactivated
    #      service does not bill an overdue backlog). Amendment 8's whole point:
    #      billing must never depend on an installed automation.
    #   2. The ENQUEUE_PROVISIONING step — the lifecycle endpoint enqueues the
    #      topology's SUSPENSION/REACTIVATION playbook directly.
    #
    # Their v4 idempotency keys ('suspend-{id}'/'reactivate-{id}') would dedupe a
    # duplicate enqueue ONLY while the native path composes byte-identical keys,
    # and not at all for the pre-v4 installed shape, which carries no key. Retiring
    # is the durable fix rather than a guarantee resting on two string literals in
    # different repos staying in sync.
    _wt(
        "plan-change", "Plan Upgrade / Downgrade",
        "When a service's plan changes, run the plan-change playbook with the new plan id.",
        "billing",
        [{"key": "plan_change_playbook_id", "label": "Plan change playbook", "type": "playbook", "required": True}],
        [{"resource_type": "client_service", "event_type": "UPDATED",
          "field_conditions": {"field": "service_plan_id", "operator": "changed"}}],
        [{"ref": "provision", "name": "Apply new plan on the network", "action_type": "ENQUEUE_PROVISIONING",
          "action_config": {"playbook_id": "{{param:plan_change_playbook_id}}",
                            "client_service_id": "{{trigger.resource_id}}",
                            "variables": {"input.client_service_id": "{{trigger.resource_id}}",
                                          "input.service_plan_id": "{{trigger.after.service_plan_id}}"}}}],
        [],
    ),
    # 'service-removal' REMOVED (service-lifecycle cycle, founder decision 8):
    # cancelling a service now natively cancels billing and enqueues the
    # topology's DEPROVISION playbook, in the cancel handler itself. Keeping
    # the template would double-fire the deprovision job on every cancel for
    # any tenant that had installed it. Retire-only, same treatment as
    # fiber-cut/maintenance below: deleted from this list, key added to
    # RETIRED_TEMPLATE_KEYS, and the retirement pass at the bottom of
    # _seed_workflow_templates deactivates the TEMPLATE row on the next migrate
    # (revision lc1_retire_removal_tmpl replays the seed in prod).
    #
    # The retirement pass does NOT touch already-installed tenant copies, and
    # cannot: installing a template materializes an independent `workflow` row
    # with no template_id/key back-reference, and find_matching_workflows
    # filters on workflow.is_active alone. Deactivating those installed copies
    # is done by lc1_retire_removal_tmpl's upgrade() — see that revision's
    # docstring for the targeting rules and why a stale copy breaks the ADMIN
    # force-cancel guarantee.
    _wt(
        "onu-replacement", "ONU / Equipment Replacement",
        "When customer equipment is reassigned to a service, run the equipment provisioning playbook with its serial.",
        "network",
        [{"key": "equipment_playbook_id", "label": "Equipment provisioning playbook", "type": "playbook", "required": True}],
        [{"resource_type": "inventory_item", "event_type": "UPDATED",
          "field_conditions": {"field": "client_service_id", "operator": "changed"}}],
        [{"ref": "provision", "name": "Provision replacement equipment", "action_type": "ENQUEUE_PROVISIONING",
          "action_config": {"playbook_id": "{{param:equipment_playbook_id}}",
                            "inventory_item_id": "{{trigger.resource_id}}",
                            "client_service_id": "{{trigger.after.client_service_id}}",
                            "variables": {"input.serial_number": "{{trigger.after.serial_number}}",
                                          "input.mac_address": "{{trigger.after.mac_address}}"}}}],
        [],
    ),
    # 'fiber-cut' and 'maintenance' REMOVED (Cycle 2 D6): both triggered on
    # resource_type='network_node', which no longer exists in
    # workflow_engine.KNOWN_RESOURCE_TYPES once the graph is dropped
    # (revision c2d_graph_removal). Retire-only per doc 18 D-- "fiber-cut/
    # maintenance templates: retire only (deactivate; no replacement this
    # cycle)" — the retirement pass below deactivates any already-seeded rows
    # for keys no longer in this list.
]

# Keys the retirement pass must never touch even though they are not (yet)
# published at every migration position — kept explicit so a future template
# add/remove doesn't need to touch the retirement logic itself.
RETIRED_TEMPLATE_KEYS = [
    "fiber-cut",
    "maintenance",
    # service-lifecycle cycle: superseded by native lifecycle handling. The
    # TEMPLATE rows are deactivated by the convergent retirement pass below;
    # already-INSTALLED tenant copies are deactivated by revisions
    # lc1_retire_removal_tmpl / lc2_retire_susp_react, because an installed
    # workflow is an independent row with no link back to its template.
    "service-removal",
    "suspension",
    "reactivation",
]

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
# (key, display name, sort order, tier, is_passive)
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
DEVICE_CATEGORIES = [
    ('ROUTER', 'Router', 10, 'CORE', False), ('SWITCH', 'Switch', 20, 'CORE', False),
    ('OLT', 'OLT', 30, 'CORE', False),
    ('ONU', 'ONU / ONT', 40, 'EDGE', False), ('SPLITTER', 'Splitter', 50, None, True),
    ('SPLICE_CLOSURE', 'Splice Closure', 60, None, True),
    ('PATCH_PANEL', 'Patch Panel', 70, None, True),
    ('ACCESS_POINT', 'Access Point', 80, 'EDGE', False),
    ('CPE_ROUTER', 'CPE Router', 90, 'EDGE', False), ('UPS', 'UPS', 100, None, False),
    ('ANTENNA', 'Antenna', 110, None, True),
    ('RADIO', 'Radio', 120, None, False), ('OTHER', 'Other', 130, None, False),
]


def seed_isp_data(connection: Connection) -> None:
    """Seed ISP permissions, roles, tier modules, templates and device categories."""
    _seed_permissions(connection)
    _seed_roles(connection)
    _seed_tier_modules(connection)
    _seed_workflow_templates(connection)
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
    # ADMIN and MANAGER inherit all new permissions (matching rbac_seed
    # policy) — except MANAGER skips the ADMIN_ONLY_PERMISSIONS keys.
    all_names = [p["name"] for p in ISP_PERMISSIONS]
    for role_name in ("ADMIN", "MANAGER"):
        names = all_names if role_name == "ADMIN" else [
            n for n in all_names if n not in ADMIN_ONLY_PERMISSIONS
        ]
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = :role AND r.company_id IS NULL AND p.name = ANY(:names) "
                "ON CONFLICT DO NOTHING"
            ),
            {"role": role_name, "names": names},
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


def _seed_workflow_templates(connection: Connection) -> None:
    # Upsert (doc 16 §5.4): templates are global blueprints; installed
    # workflows are materialized copies, so DO UPDATE is safe and lets template
    # revisions (e.g. new-installation v2, suspension/reactivation/
    # service-removal v3) ship without a new key. Release note: tenants
    # reinstall to pick up a new version.
    #
    # Migration-position gate: seeds run after ANY alembic command (including
    # partial upgrades and downgrades), but a template definition may use step
    # action types (or, per Cycle 2, columns) added by a LATER revision.
    # Publishing such a blueprint before the DB can represent it makes install
    # (or the first trigger fire) 500 — skip templates the database cannot
    # support yet, at whatever granularity is needed:
    #   1. stepactiontype enum membership (pre-existing, doc 16 §5.4/43c543c)
    #   2. required columns (Cycle 2, doc 18 amendment 10) — e.g. v3
    #      suspension/reactivation/service-removal need
    #      client_service.billing_status, which only exists from c2b onward
    #   3. trigger resource types must all be in the engine's KNOWN_RESOURCE_TYPES
    #      (Cycle 2, topology-networking verifier fix) — catches a template
    #      whose trigger references a resource the running engine build
    #      cannot resolve at all (e.g. a template authored against a resource
    #      type removed by a later revision, or not yet added by an earlier one)
    supported_actions = {
        row[0]
        for row in connection.execute(text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'stepactiontype'"
        ))
    }
    # The engine's model map as of THIS build, not migration-position-dependent.
    known_resource_types = set(KNOWN_RESOURCE_TYPES)
    seeded_keys = []

    for tpl in WORKFLOW_TEMPLATES:
        required_actions = {
            step.get("action_type")
            for step in tpl["definition"].get("steps", [])
            if step.get("action_type")
        }
        missing_actions = required_actions - supported_actions
        if missing_actions:
            logger.warning(
                f"Skipping template '{tpl['key']}': stepactiontype enum lacks "
                f"{sorted(missing_actions)} at this migration position"
            )
            continue

        missing_columns = []
        for table, column in TEMPLATE_REQUIRED_COLUMNS.get(tpl["key"], []):
            exists = connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            ).fetchone()
            if not exists:
                missing_columns.append(f"{table}.{column}")
        if missing_columns:
            logger.warning(
                f"Skipping template '{tpl['key']}': missing column(s) "
                f"{missing_columns} at this migration position"
            )
            continue

        trigger_resource_types = {
            trig.get("resource_type")
            for trig in tpl["definition"].get("triggers", [])
            if trig.get("resource_type")
        }
        unknown_resources = trigger_resource_types - known_resource_types
        if unknown_resources:
            logger.warning(
                f"Skipping template '{tpl['key']}': trigger resource type(s) "
                f"{sorted(unknown_resources)} are not in KNOWN_RESOURCE_TYPES"
            )
            continue

        connection.execute(
            text(
                "INSERT INTO workflow_template (id, created_at, key, name, description, category, definition, is_active) "
                "VALUES (gen_random_uuid(), :created_at, :key, :name, :description, :category, :definition, TRUE) "
                "ON CONFLICT (key) DO UPDATE SET "
                "name = EXCLUDED.name, description = EXCLUDED.description, "
                "category = EXCLUDED.category, definition = EXCLUDED.definition, "
                "is_active = TRUE"
            ),
            {
                "created_at": now_gt(),
                "key": tpl["key"],
                "name": tpl["name"],
                "description": tpl["description"],
                "category": tpl["category"],
                "definition": json.dumps(tpl["definition"]),
            },
        )
        seeded_keys.append(tpl["key"])

    # Retirement pass (doc 18 amendment 10 / topology-networking verifier
    # fix): templates are exclusively seed-owned (install is the only other
    # write path and never touches workflow_template rows), so deactivating
    # every key NOT in this run's seeded set is safe and convergent on every
    # migrate — this is what actually retires 'fiber-cut'/'maintenance' (and
    # any future removed key) without a destructive DELETE, and it also
    # covers templates skipped above by the gates (they must not stay active
    # with a stale pre-gate definition).
    if seeded_keys:
        connection.execute(
            text("UPDATE workflow_template SET is_active = FALSE WHERE key <> ALL(:seeded_keys)"),
            {"seeded_keys": seeded_keys},
        )
    logger.info(f"Seeded {len(seeded_keys)}/{len(WORKFLOW_TEMPLATES)} workflow templates")


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

    for key, name, sort_order, tier, _is_passive in DEVICE_CATEGORIES:
        if has_tier:
            connection.execute(
                text(
                    "INSERT INTO device_category (id, key, name, sort_order, tier, is_active, is_system, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :key, :name, :sort_order, :tier, TRUE, TRUE, :created_at, :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": key, "name": name, "sort_order": sort_order, "tier": tier, "created_at": now_gt()},
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO device_category (id, key, name, sort_order, is_active, is_system, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :key, :name, :sort_order, TRUE, TRUE, :created_at, :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": key, "name": name, "sort_order": sort_order, "created_at": now_gt()},
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
            for key, _name, _sort_order, tier, _passive in DEVICE_CATEGORIES:
                if tier is None:
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
            for key, _name, _sort_order, _tier, is_passive in DEVICE_CATEGORIES:
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
