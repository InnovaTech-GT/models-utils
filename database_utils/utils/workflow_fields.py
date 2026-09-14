"""
Workflow field metadata: hardcoded allowlist of editable fields per resource type.

Used by the workflow UI to populate field dropdowns in triggers and steps,
and by the engine to validate field access.
"""
from typing import List, Dict, Any


# Each entry: {"name": "<column_name>", "type": "<field_type>", "fk_to": "<resource_type>" | None}
# type values: "string", "number", "boolean", "date", "uuid", "json"
# Optional key `"writable": False` marks TRIGGER-CONDITION-ONLY fields: they may
# appear in trigger field_conditions but are rejected by the engine's
# UPDATE_FIELD denylist — UI field pickers for UPDATE_FIELD steps must filter
# them out (doc 16 §2.2/§5.3). Absent key means writable.
RESOURCE_FIELDS: Dict[str, List[Dict[str, Any]]] = {
    # NOTE (doc 16 §2.2/§5.3): `paid` removed — payment state is written only by
    # backend-erp's PaymentService (single-writer invariant). payment_status and
    # order_type are exposed for TRIGGER CONDITIONS only; the engine's
    # UPDATE_FIELD denylist (workflow_engine.py) blocks writing them.
    "order": [
        {"name": "due_date", "type": "date", "fk_to": None},
        {"name": "total", "type": "number", "fk_to": None, "writable": False},
        {"name": "status", "type": "string", "fk_to": None},
        {"name": "payment_status", "type": "string", "fk_to": None, "writable": False},
        {"name": "order_type", "type": "string", "fk_to": None, "writable": False},
        {"name": "client_id", "type": "uuid", "fk_to": "client"},
        {"name": "recurring_order_id", "type": "uuid", "fk_to": "recurring_order"},
    ],
    "client": [
        {"name": "name", "type": "string", "fk_to": None},
        {"name": "tax_id", "type": "string", "fk_to": None},
        {"name": "address", "type": "string", "fk_to": None},
        {"name": "phone", "type": "string", "fk_to": None},
        {"name": "email", "type": "string", "fk_to": None},
        {"name": "contact", "type": "string", "fk_to": None},
        {"name": "observations", "type": "string", "fk_to": None},
        {"name": "advisor_id", "type": "uuid", "fk_to": None},
        {"name": "latitude", "type": "number", "fk_to": None},
        {"name": "longitude", "type": "number", "fk_to": None},
        {"name": "service_availability", "type": "string", "fk_to": None},
        # installation_status/installation_date removed (cf1): the columns are
        # dropped — install truth is client_service.install_state, and cf1's
        # data pass deletes any installed UPDATE_FIELD step still writing them.
        {"name": "assigned_technician_id", "type": "uuid", "fk_to": None},
    ],
    "product": [
        {"name": "name", "type": "string", "fk_to": None},
        {"name": "price", "type": "number", "fk_to": None},
        {"name": "description", "type": "string", "fk_to": None},
        {"name": "stock", "type": "number", "fk_to": None},
    ],
    "task": [
        {"name": "name", "type": "string", "fk_to": None},
        {"name": "description", "type": "string", "fk_to": None},
        {"name": "position", "type": "number", "fk_to": None},
        {"name": "due_date", "type": "date", "fk_to": None},
        {"name": "task_state_id", "type": "uuid", "fk_to": "task_state"},
    ],
    "task_state": [
        {"name": "name", "type": "string", "fk_to": None},
        {"name": "color", "type": "string", "fk_to": None},
        {"name": "position", "type": "number", "fk_to": None},
    ],
    "recurring_order": [
        {"name": "recurrence", "type": "string", "fk_to": None},
        {"name": "recurrence_end", "type": "date", "fk_to": None},
        {"name": "status", "type": "string", "fk_to": None},
        {"name": "client_id", "type": "uuid", "fk_to": "client"},
    ],
    # Invoice money/validity is written only by PaymentService (doc 16 §1);
    # these stay visible for trigger conditions but are engine-denylisted.
    "invoice": [
        {"name": "issue_date", "type": "date", "fk_to": None},
        {"name": "subtotal", "type": "number", "fk_to": None, "writable": False},
        {"name": "tax", "type": "number", "fk_to": None, "writable": False},
        {"name": "total", "type": "number", "fk_to": None, "writable": False},
        {"name": "details", "type": "json", "fk_to": None},
        {"name": "is_valid", "type": "boolean", "fk_to": None, "writable": False},
        {"name": "order_id", "type": "uuid", "fk_to": "order"},
    ],
    "order_item": [
        {"name": "order_id", "type": "uuid", "fk_to": "order"},
        {"name": "product_id", "type": "uuid", "fk_to": "product"},
        {"name": "quantity", "type": "number", "fk_to": None},
    ],
    # --- ISP resources ---
    "service_plan": [
        {"name": "name", "type": "string", "fk_to": None},
        {"name": "plan_type", "type": "string", "fk_to": None},
        {"name": "download_mbps", "type": "number", "fk_to": None},
        {"name": "upload_mbps", "type": "number", "fk_to": None},
        {"name": "price", "type": "number", "fk_to": None},
        {"name": "is_active", "type": "boolean", "fk_to": None},
        # Cycle 2 D1/D2: what this plan bills for (drives order_type derivation).
        {"name": "kind", "type": "string", "fk_to": None},
    ],
    "client_service": [
        {"name": "status", "type": "string", "fk_to": None},
        {"name": "activation_date", "type": "date", "fk_to": None},
        {"name": "notes", "type": "string", "fk_to": None},
        {"name": "client_id", "type": "uuid", "fk_to": "client"},
        {"name": "service_plan_id", "type": "uuid", "fk_to": "service_plan"},
        # Cycle 2 D5: replaces network_node_id (removed, revision
        # c2d_graph_removal).
        {"name": "cpe_item_id", "type": "uuid", "fk_to": "inventory_item"},
        # Cycle 2 D1 billing absorption (doc 18 amendment 8): writable — the
        # rewritten suspension/reactivation/service-removal templates
        # UPDATE_FIELD billing_status directly (replacing the pre-merge
        # recurring_order.status step). quantity/recurrence/dates are exposed
        # for completeness (e.g. a future "extend recurrence_end" automation).
        {"name": "recurrence", "type": "string", "fk_to": None},
        {"name": "recurrence_end", "type": "date", "fk_to": None},
        {"name": "next_generation_date", "type": "date", "fk_to": None},
        {"name": "last_generated_at", "type": "date", "fk_to": None},
        {"name": "billing_status", "type": "string", "fk_to": None},
        {"name": "quantity", "type": "number", "fk_to": None},
        # migration-critical bridge state (doc 18 amendment 1) — visible for
        # trigger conditions only; the engine's UPDATE_FIELD denylist
        # (workflow_engine.py) blocks writing it. migration_source is
        # intentionally NOT listed here at all: it is pure internal audit
        # bookkeeping with no legitimate automation use, read or write.
        {"name": "recurring_order_id", "type": "uuid", "fk_to": "recurring_order", "writable": False},
    ],
    "service_suspension": [
        {"name": "reason", "type": "string", "fk_to": None},
        {"name": "note", "type": "string", "fk_to": None},
        {"name": "reactivated_at", "type": "date", "fk_to": None},
        {"name": "client_service_id", "type": "uuid", "fk_to": "client_service"},
    ],
    "inventory_item": [
        {"name": "serial_number", "type": "string", "fk_to": None},
        {"name": "mac_address", "type": "string", "fk_to": None},
        {"name": "status", "type": "string", "fk_to": None},
        {"name": "condition", "type": "string", "fk_to": None},
        {"name": "notes", "type": "string", "fk_to": None},
        {"name": "warehouse_id", "type": "uuid", "fk_to": None},
        {"name": "client_id", "type": "uuid", "fk_to": "client"},
        {"name": "client_service_id", "type": "uuid", "fk_to": "client_service"},
    ],
    # 'network_node' resource REMOVED (Cycle 2 D6, revision
    # c2d_graph_removal) — the free-form network graph no longer exists.
    "provisioning_job": [
        {"name": "status", "type": "string", "fk_to": None},
        {"name": "error", "type": "string", "fk_to": None},
        {"name": "client_service_id", "type": "uuid", "fk_to": "client_service"},
    ],
}


def get_resource_fields(resource_type: str) -> List[Dict[str, Any]]:
    """Return all editable fields for a given resource type."""
    return RESOURCE_FIELDS.get(resource_type, [])


def get_writable_fields(resource_type: str) -> List[Dict[str, Any]]:
    """Return only fields valid as UPDATE_FIELD targets (excludes
    trigger-condition-only fields marked writable=False)."""
    return [f for f in get_resource_fields(resource_type) if f.get("writable", True)]

