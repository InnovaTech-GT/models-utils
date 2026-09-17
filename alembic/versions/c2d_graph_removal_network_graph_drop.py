"""cycle 2 D6: remove the free-form network graph (network_node/network_node_type/network_link)

Revision ID: c2d_graph_removal
Revises: c2c_topology
Create Date: 2026-07-06

Doc 18-cycle2-design.md D6/D7 + 18a-cycle2-design-appendix.md topology-
networking §2, amendment 11. The network graph (map UI, node/link/node_type)
is removed entirely — Topology (c2c) replaces it for provisioning purposes;
D7: device-side lat/long dies with it (client-side coordinates already exist
and are untouched).

Step order is load-bearing (appendix §2, verifier issue on task_template):
1. NULL every NETWORK_NODE link on BOTH task.linked_object_type/
   linked_object_id AND task_template.linked_object_type — these are the only
   two Enum(TaskLinkedObjectType) columns (grep-verified) and BOTH must be
   nulled before the Python enum member disappears (it already has, in this
   same models-utils commit — crm.py), or ANY ORM read of a row still holding
   'NETWORK_NODE' raises a LookupError coercing it. The PG enum VALUE itself
   is permanent (Postgres cannot DROP a label) — documented as irreversible-
   but-harmless, same precedent as c1e_install_actions.
2. Disable installed (per-tenant) workflows that reference network_node,
   either via a trigger (deleted) or via any step's action_config (broadened
   per verifier fix — a tenant-modified multi-trigger copy could otherwise
   stay "active" while perpetually failing on every fire).
3. Retire (deactivate only — founder decision: no replacement this cycle)
   the 'fiber-cut'/'maintenance' seeded templates.
4. Drop the three network_node_id columns (client_service, inventory_item,
   provisioning_job) BEFORE the tables — required so DROP TABLE network_node
   isn't blocked by a referencing FK.
5. Drop network_link, then network_node (carries the cd2f cycle-breaker FK
   fk_network_node_inventory_item — dropped automatically with the table),
   then network_node_type, then the networknodestatus enum.
6. Remove the 11 network_* permission rows (+ role_permission cascade) and
   rewrite tier.modules 'network' -> 'topologies' (amendment 14) for any
   already-seeded rows (append-only isp_seed.py cannot rewrite existing JSON).

Local/rehearsal DBs have seeded rows for all of the above (network_node_type
default set, fiber-cut/maintenance-triggering workflows, etc.) — PROD is
still pre-ISP (head a1f2b3c4d5e6) and has ZERO rows in every table this
revision touches, so plain (non-IF-EXISTS-guarded) drops are safe there. IF
EXISTS guards are still used on drops for defensive re-run safety.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c2d_graph_removal'
down_revision: Union[str, Sequence[str], None] = 'c2c_topology'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NETWORK_PERMISSION_NAMES = [
    'network_node_types.create', 'network_node_types.read',
    'network_node_types.update', 'network_node_types.delete',
    'network_nodes.create', 'network_nodes.read',
    'network_nodes.update', 'network_nodes.delete',
    'network_links.create', 'network_links.read', 'network_links.delete',
]

_RETIRED_TEMPLATE_KEYS = ['fiber-cut', 'maintenance']


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. Enum-link sweep (amendment 11) — BOTH tables, task first. ---
    nulled_tasks = connection.execute(text(
        "UPDATE task SET linked_object_type = NULL, linked_object_id = NULL "
        "WHERE linked_object_type = 'NETWORK_NODE'"
    )).rowcount
    nulled_templates = connection.execute(text(
        "UPDATE task_template SET linked_object_type = NULL WHERE linked_object_type = 'NETWORK_NODE'"
    )).rowcount
    print(f"[c2d_graph_removal] nulled NETWORK_NODE links: task={nulled_tasks}, task_template={nulled_templates}")

    # --- 2. Disable installed workflows referencing network_node. ---
    trigger_affected = connection.execute(text(
        "DELETE FROM workflow_trigger WHERE resource_type = 'network_node' RETURNING workflow_id"
    )).fetchall()
    step_affected = connection.execute(text(
        "SELECT DISTINCT workflow_id FROM workflow_step "
        "WHERE action_config::text ILIKE '%network_node%' OR action_config::text LIKE '%NETWORK_NODE%'"
    )).fetchall()
    affected_ids = {row[0] for row in trigger_affected} | {row[0] for row in step_affected}
    if affected_ids:
        connection.execute(
            text("UPDATE workflow SET is_active = FALSE WHERE id = ANY(:ids)"),
            {"ids": list(affected_ids)},
        )
    print(f"[c2d_graph_removal] deactivated {len(affected_ids)} installed workflow(s) "
          f"referencing network_node (local/rehearsal only — prod has none)")

    # --- 3. Retire (deactivate only, founder decision — no replacement). ---
    retired = connection.execute(
        text("UPDATE workflow_template SET is_active = FALSE WHERE key = ANY(:keys) RETURNING key"),
        {"keys": _RETIRED_TEMPLATE_KEYS},
    ).fetchall()
    print(f"[c2d_graph_removal] retired templates: {[r[0] for r in retired]}")

    # --- 4. Drop network_node_id columns (before the tables they FK to). ---
    op.drop_column('client_service', 'network_node_id')
    op.drop_column('inventory_item', 'network_node_id')
    op.drop_column('provisioning_job', 'network_node_id')

    # --- 5. Drop the graph tables (link -> node -> node_type order) + enum. ---
    op.execute('DROP TABLE IF EXISTS network_link CASCADE')
    op.execute('DROP TABLE IF EXISTS network_node CASCADE')
    op.execute('DROP TABLE IF EXISTS network_node_type CASCADE')
    sa.Enum(name='networknodestatus').drop(connection, checkfirst=True)
    # tasklinkedobjecttype's NETWORK_NODE PG enum VALUE is permanent
    # (Postgres has no ALTER TYPE ... DROP VALUE) — irreversible-but-harmless,
    # same precedent as c1e_install_actions's CREATE_ORDER/CREATE_TASK values.

    # --- 6. RBAC + tier module convergent removal (local-only rows; prod
    # never seeded them — pre-ISP head). ---
    connection.execute(text(
        "DELETE FROM role_permission WHERE permission_id IN "
        "(SELECT id FROM permission WHERE name = ANY(:names))"
    ), {"names": _NETWORK_PERMISSION_NAMES})
    deleted_perms = connection.execute(
        text("DELETE FROM permission WHERE name = ANY(:names) RETURNING name"),
        {"names": _NETWORK_PERMISSION_NAMES},
    ).fetchall()
    print(f"[c2d_graph_removal] removed {len(deleted_perms)} network_* permission(s)")

    tier_rows = connection.execute(text("SELECT id, modules FROM tier")).fetchall()
    rewritten_tiers = 0
    for tier_id, modules in tier_rows:
        current = modules or []
        if isinstance(current, str):
            current = json.loads(current)
        if 'network' not in current:
            continue
        # Order-preserving replace (dedupe if 'topologies' already present).
        replaced = ['topologies' if m == 'network' else m for m in current]
        deduped = list(dict.fromkeys(replaced))
        connection.execute(
            text("UPDATE tier SET modules = :modules WHERE id = :id"),
            {"modules": json.dumps(deduped), "id": tier_id},
        )
        rewritten_tiers += 1
    print(f"[c2d_graph_removal] rewrote tier.modules 'network'->'topologies' for {rewritten_tiers} tier(s) "
          f"(prod never seeded ISP modules pre-cycle2 — local/rehearsal only)")


def downgrade() -> None:
    """Structurally recreate the three tables + three columns (cd2f's exact
    definitions) + the networknodestatus enum. Data is NOT restored:
    - prod never had any rows in these tables (pre-ISP head) — zero loss there.
    - local/rehearsal seeded rows (default node types, fiber-cut/maintenance
      triggers, etc.) are gone — the seed no longer creates node types
      (_seed_default_node_types was deleted in this same commit) so they do
      NOT come back on the next migrate either. This is documented and
      accepted (D6 removes the feature; ZERO DATA LOSS applies to prod).
    """
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    sa.Enum('PLANNED', 'ACTIVE', 'DEGRADED', 'DOWN', 'MAINTENANCE',
            name='networknodestatus').create(connection, checkfirst=True)

    op.create_table(
        'network_node_type',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        # postgresql.ENUM, not sa.Enum: only the PG dialect type honors
        # create_type=False. devicecategory is NEVER dropped by this revision's
        # upgrade (device_type still uses it), so recreating it here would
        # raise DuplicateObject.
        sa.Column('category', PGEnum(
            'ROUTER', 'SWITCH', 'OLT', 'ONU', 'SPLITTER', 'SPLICE_CLOSURE',
            'PATCH_PANEL', 'ACCESS_POINT', 'CPE_ROUTER', 'UPS', 'ANTENNA',
            'RADIO', 'OTHER', name='devicecategory', create_type=False,
        ), nullable=True),
        sa.Column('icon', sa.String(), nullable=True),
        sa.Column('allowed_parent_keys', sa.JSON(), nullable=True),
        sa.Column('attribute_schema', sa.JSON(), nullable=True),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'key', name='uq_network_node_type_company_key'),
    )
    op.create_index(op.f('ix_network_node_type_company_id'), 'network_node_type', ['company_id'], unique=False)

    op.create_table(
        'network_node',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('status', PGEnum(name='networknodestatus', create_type=False),
                  server_default='ACTIVE', nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('capacity', sa.Integer(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('node_type_id', sa.Uuid(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
        sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['node_type_id'], ['network_node_type.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['parent_id'], ['network_node.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_network_node_company_id'), 'network_node', ['company_id'], unique=False)
    op.create_index(op.f('ix_network_node_parent_id'), 'network_node', ['parent_id'], unique=False)

    op.create_table(
        'network_link',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('link_type', sa.String(), nullable=False),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('from_node_id', sa.Uuid(), nullable=False),
        sa.Column('to_node_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['from_node_id'], ['network_node.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_node_id'], ['network_node.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_network_link_company_id'), 'network_link', ['company_id'], unique=False)

    # Cycle-breaker FK, named exactly as cd2f (its own downgrade drops it by name).
    op.create_foreign_key(
        'fk_network_node_inventory_item', 'network_node', 'inventory_item',
        ['inventory_item_id'], ['id'], ondelete='SET NULL',
    )

    op.add_column('client_service', sa.Column('network_node_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'client_service_network_node_id_fkey', 'client_service', 'network_node',
        ['network_node_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column('inventory_item', sa.Column('network_node_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'inventory_item_network_node_id_fkey', 'inventory_item', 'network_node',
        ['network_node_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column('provisioning_job', sa.Column('network_node_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'provisioning_job_network_node_id_fkey', 'provisioning_job', 'network_node',
        ['network_node_id'], ['id'], ondelete='SET NULL',
    )

    print("[c2d_graph_removal] downgrade: tables/columns recreated EMPTY — no data restored "
          "(prod had none; local seed no longer creates node types — documented, acceptable)")
