"""cycle 10: the company network graph — additive half (doc 35 §8)

Revision ID: ng1_network_graph
Revises: lc2_retire_susp_react
Create Date: 2026-08-06

Strictly ADDITIVE. Nothing is dropped, nothing is rewritten, nothing is even
read. The destructive half — guards, the workflow-config rewrite, and the
Topology drops — is ng2_topology_drop, deliberately a separate revision so a
reviewer can read "what appears" and "what disappears" independently.

What this adds:

1. inventory_item.parent_id + network_attached — the plant becomes a tree of
   inventory items (doc 35 §2.1). RESTRICT on delete: removing an OLT must not
   silently promote the subscribers behind it to roots.
2. device_category.is_passive — signal-passive gear that is ON the path but
   never configured (doc 35 §2.3).
3. device_type_playbook / inventory_item_playbook — playbooks bind to
   equipment, overridable per node (doc 35 §2.4).
4. client_service.cpe_item_id + path_changed_at (doc 35 §2.5, §5.2).
5. provisioning_run + provisioning_job.run_id/run_position (doc 35 §5).
6. trg_inventory_item_graph_guard + trg_inventory_item_detach_guard.

WHY THE GUARDS ARE TRIGGERS. A CHECK constraint cannot express reachability, so
"a node may not become its own ancestor" and "a parent must belong to the same
company" are unstateable as CHECKs. The service layer runs the same checks first
so the operator gets a readable 422; these triggers are the guarantee, and they
are what makes cross-tenant traversal impossible rather than merely unlikely.

They live only here, never in SQLAlchemy metadata: the test suites of all four
consuming services build their schemas with SQLite create_all, which cannot
parse plpgsql. Same precedent as ck_topology_playbook_purpose_format and
nc1b_device_audit_trigger.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum, JSON

revision: str = "ng1_network_graph"
down_revision: Union[str, None] = "lc2_retire_susp_react"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with database_utils/utils/network_graph.MAX_PATH_DEPTH. If these
# ever disagree the trigger wins, and traversal starts raising PATH_TOO_DEEP on
# paths the database happily accepted.
MAX_PATH_DEPTH = 32

_GRAPH_GUARD_FN = f"""
CREATE OR REPLACE FUNCTION inventory_item_graph_guard() RETURNS trigger AS $$
DECLARE
    parent_company uuid;
    parent_attached boolean;
    cycle_found boolean;
    deepest int;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION 'NETWORK_GRAPH_SELF_PARENT: % cannot be its own parent',
            NEW.id;
    END IF;

    SELECT company_id, network_attached
      INTO parent_company, parent_attached
      FROM inventory_item WHERE id = NEW.parent_id;

    IF parent_company IS NULL THEN
        RAISE EXCEPTION 'NETWORK_GRAPH_PARENT_NOT_FOUND: %', NEW.parent_id;
    END IF;
    IF parent_company <> NEW.company_id THEN
        RAISE EXCEPTION
            'NETWORK_GRAPH_CROSS_TENANT: parent % belongs to another company',
            NEW.parent_id;
    END IF;
    IF NOT parent_attached THEN
        RAISE EXCEPTION
            'NETWORK_GRAPH_PARENT_DETACHED: parent % is not attached to the graph',
            NEW.parent_id;
    END IF;

    -- Walk the prospective parent's ancestry. The depth bound is not
    -- decoration: an unbounded recursive CTE over a pre-existing cycle does not
    -- error, it hangs, and this runs on the provisioning hot path.
    WITH RECURSIVE anc(id, parent_id, depth) AS (
        SELECT i.id, i.parent_id, 1
          FROM inventory_item i
         WHERE i.id = NEW.parent_id AND i.company_id = NEW.company_id
        UNION ALL
        SELECT i.id, i.parent_id, anc.depth + 1
          FROM inventory_item i
          JOIN anc ON i.id = anc.parent_id
         WHERE i.company_id = NEW.company_id AND anc.depth < {MAX_PATH_DEPTH}
    )
    SELECT bool_or(id = NEW.id), COALESCE(max(depth), 0)
      INTO cycle_found, deepest
      FROM anc;

    IF cycle_found THEN
        RAISE EXCEPTION 'NETWORK_GRAPH_CYCLE: % would become its own ancestor',
            NEW.id;
    END IF;
    IF deepest >= {MAX_PATH_DEPTH} THEN
        RAISE EXCEPTION 'NETWORK_GRAPH_TOO_DEEP: depth % exceeds {MAX_PATH_DEPTH}',
            deepest;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DETACH_GUARD_FN = """
CREATE OR REPLACE FUNCTION inventory_item_detach_guard() RETURNS trigger AS $$
BEGIN
    -- Detaching a node with children would strand them: their parent_id would
    -- point at something outside the graph, so resolve_path would stop early
    -- and every subscriber behind it would silently resolve a shorter path.
    IF OLD.network_attached AND NOT NEW.network_attached
       AND EXISTS (SELECT 1 FROM inventory_item WHERE parent_id = OLD.id) THEN
        RAISE EXCEPTION
            'NETWORK_GRAPH_HAS_CHILDREN: re-parent or detach the children of % first',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_PURPOSE_CHECK = "purpose ~ '^[A-Z][A-Z0-9_]{0,49}$'"


def upgrade() -> None:
    # --- 1. the tree ------------------------------------------------------
    op.add_column("inventory_item", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.add_column(
        "inventory_item",
        sa.Column("network_attached", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    op.create_foreign_key(
        "fk_inventory_item_parent", "inventory_item", "inventory_item",
        ["parent_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_inventory_item_parent_id", "inventory_item", ["parent_id"])
    op.create_index(
        "ix_inventory_item_company_attached", "inventory_item", ["company_id"],
        postgresql_where=sa.text("network_attached"),
    )
    op.create_check_constraint(
        "ck_inventory_item_parent_attached", "inventory_item",
        "parent_id IS NULL OR network_attached",
    )
    op.create_check_constraint(
        "ck_inventory_item_not_self_parent", "inventory_item",
        "parent_id IS NULL OR parent_id <> id",
    )

    # --- 2. passive categories -------------------------------------------
    op.add_column(
        "device_category",
        sa.Column("is_passive", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )

    # --- 3. playbook binding ---------------------------------------------
    op.create_table(
        "device_type_playbook",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("device_type_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("playbook_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_type_id"], ["device_type.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["playbook_id"], ["playbook.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("device_type_id", "purpose",
                            name="uq_device_type_playbook_purpose"),
        sa.CheckConstraint(_PURPOSE_CHECK, name="ck_device_type_playbook_purpose_format"),
    )
    op.create_index("ix_device_type_playbook_company_id", "device_type_playbook",
                    ["company_id"])
    op.create_index("ix_device_type_playbook_playbook_id", "device_type_playbook",
                    ["playbook_id"])

    op.create_table(
        "inventory_item_playbook",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_item_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("playbook_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_item.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["playbook_id"], ["playbook.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("inventory_item_id", "purpose",
                            name="uq_item_playbook_purpose"),
        sa.CheckConstraint(_PURPOSE_CHECK, name="ck_item_playbook_purpose_format"),
    )
    op.create_index("ix_inventory_item_playbook_company_id", "inventory_item_playbook",
                    ["company_id"])
    op.create_index("ix_inventory_item_playbook_playbook_id", "inventory_item_playbook",
                    ["playbook_id"])

    # --- 4. the service's two network inputs ------------------------------
    op.add_column("client_service", sa.Column("cpe_item_id", sa.Uuid(), nullable=True))
    op.add_column("client_service",
                  sa.Column("path_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_client_service_cpe_item", "client_service", "inventory_item",
        ["cpe_item_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_client_service_cpe_item_id", "client_service", ["cpe_item_id"])

    # --- 5. multi-device runs ---------------------------------------------
    op.create_table(
        "provisioning_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Both enum types already exist (provisioning_job owns them). PGEnum
        # with create_type=False reuses them; sa.Enum would try to CREATE TYPE
        # and fail with DuplicateObject.
        sa.Column(
            "status",
            PGEnum(
                "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "ROLLED_BACK",
                "CANCELLED", "PENDING_INFORM",
                name="provisioningjobstatus", create_type=False,
            ),
            nullable=False, server_default="QUEUED",
        ),
        sa.Column("path", JSON(), nullable=False),
        sa.Column("plan", JSON(), nullable=False),
        sa.Column("frames", JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column(
            "triggered_by",
            PGEnum("USER", "WORKFLOW", "SYSTEM", name="provisioningtrigger",
                   create_type=False),
            nullable=False, server_default="USER",
        ),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("client_service_id", sa.Uuid(), nullable=False),
        sa.Column("triggered_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_service_id"], ["client_service.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_provisioning_run_company_id", "provisioning_run", ["company_id"])
    op.create_index("ix_provisioning_run_client_service_id", "provisioning_run",
                    ["client_service_id"])
    op.create_index("ix_provisioning_run_service", "provisioning_run",
                    ["client_service_id", "created_at"])
    # Mirrors uq_provisioning_job_company_idem: a re-fire while a run is still
    # in flight dedupes instead of opening a second one.
    op.create_index(
        "uq_provisioning_run_company_idem", "provisioning_run",
        ["company_id", "idempotency_key"], unique=True,
        postgresql_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN "
            "('QUEUED','RUNNING','PENDING_INFORM')"
        ),
    )

    op.add_column("provisioning_job", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.add_column("provisioning_job", sa.Column("run_position", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_provisioning_job_run", "provisioning_job", "provisioning_run",
        ["run_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_provisioning_job_run_id", "provisioning_job", ["run_id"])

    # --- 6. the guards ----------------------------------------------------
    op.execute(_GRAPH_GUARD_FN)
    op.execute(_DETACH_GUARD_FN)
    op.execute("""
        CREATE TRIGGER trg_inventory_item_graph_guard
            BEFORE INSERT OR UPDATE OF parent_id ON inventory_item
            FOR EACH ROW EXECUTE FUNCTION inventory_item_graph_guard();
    """)
    op.execute("""
        CREATE TRIGGER trg_inventory_item_detach_guard
            BEFORE UPDATE OF network_attached ON inventory_item
            FOR EACH ROW EXECUTE FUNCTION inventory_item_detach_guard();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_inventory_item_detach_guard ON inventory_item")
    op.execute("DROP TRIGGER IF EXISTS trg_inventory_item_graph_guard ON inventory_item")
    op.execute("DROP FUNCTION IF EXISTS inventory_item_detach_guard()")
    op.execute("DROP FUNCTION IF EXISTS inventory_item_graph_guard()")

    op.drop_index("ix_provisioning_job_run_id", table_name="provisioning_job")
    op.drop_constraint("fk_provisioning_job_run", "provisioning_job", type_="foreignkey")
    op.drop_column("provisioning_job", "run_position")
    op.drop_column("provisioning_job", "run_id")
    op.drop_table("provisioning_run")

    op.drop_index("ix_client_service_cpe_item_id", table_name="client_service")
    op.drop_constraint("fk_client_service_cpe_item", "client_service", type_="foreignkey")
    op.drop_column("client_service", "path_changed_at")
    op.drop_column("client_service", "cpe_item_id")

    op.drop_table("inventory_item_playbook")
    op.drop_table("device_type_playbook")

    op.drop_column("device_category", "is_passive")

    op.drop_constraint("ck_inventory_item_not_self_parent", "inventory_item",
                       type_="check")
    op.drop_constraint("ck_inventory_item_parent_attached", "inventory_item",
                       type_="check")
    op.drop_index("ix_inventory_item_company_attached", table_name="inventory_item")
    op.drop_index("ix_inventory_item_parent_id", table_name="inventory_item")
    op.drop_constraint("fk_inventory_item_parent", "inventory_item", type_="foreignkey")
    op.drop_column("inventory_item", "network_attached")
    op.drop_column("inventory_item", "parent_id")
