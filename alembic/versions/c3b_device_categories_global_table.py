"""cycle 3 E4: global device_category table, enum -> FK backfill, devicecategory enum drop

Revision ID: c3b_device_categories
Revises: c3a_topology_purpose
Create Date: 2026-07-06

Doc 20-cycle3-design.md E4 + 20a-cycle3-design-appendix.md
admin-categories-sidebar §0/§1, normative amendment 9. The `devicecategory`
PG enum (13 fixed labels: ROUTER, SWITCH, OLT, ONU, SPLITTER, SPLICE_CLOSURE,
PATCH_PANEL, ACCESS_POINT, CPE_ROUTER, UPS, ANTENNA, RADIO, OTHER) becomes a
platform-global, super-admin-managed table (no company_id) so adding a
category never requires a migration. `device_type.category` and
`playbook.target_category` convert to FK columns (`category_id`/
`target_category_id`) backfilled by exact key match, then the enum type is
dropped in this SAME revision (precedent: c2d_graph_removal dropped
`networknodestatus` in the revision that removed its last user — after this
conversion `devicecategory` has zero users, both columns converted above it).

ZERO DATA LOSS: keys are byte-identical to the enum labels, so the backfill
is a total, deterministic mapping. Prod has ZERO device_type/playbook rows
(pre-ISP head) — this only moves local/rehearsal data.
"""
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c3b_device_categories'
down_revision: Union[str, Sequence[str], None] = 'c3a_topology_purpose'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen list — the 13 original devicecategory enum labels, duplicated (not
# imported) from alembic/seeds/isp_seed.py's DEVICE_CATEGORIES: revisions are
# immutable forever, while the seed's baseline list may grow in later cycles
# without a new migration. Both lists must independently start from this same
# set for the upgrade backfill (here) and the seed's convergent insert to
# agree on what "baseline" means.
CATEGORIES = [
    ('ROUTER', 'Router', 10), ('SWITCH', 'Switch', 20), ('OLT', 'OLT', 30),
    ('ONU', 'ONU', 40), ('SPLITTER', 'Splitter', 50), ('SPLICE_CLOSURE', 'Splice Closure', 60),
    ('PATCH_PANEL', 'Patch Panel', 70), ('ACCESS_POINT', 'Access Point', 80),
    ('CPE_ROUTER', 'CPE Router', 90), ('UPS', 'UPS', 100), ('ANTENNA', 'Antenna', 110),
    ('RADIO', 'Radio', 120), ('OTHER', 'Other', 130),
]
_CATEGORY_LABELS = [c[0] for c in CATEGORIES]


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. table ---
    op.create_table(
        'device_category',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('key', sa.String(50), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('icon', sa.String(50), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key', name='uq_device_category_key'),
    )

    # --- 2. seed the 13 system rows (targets for the backfill below) ---
    for key, name, sort_order in CATEGORIES:
        connection.execute(
            text(
                "INSERT INTO device_category (id, key, name, sort_order, is_active, is_system, created_at, updated_at) "
                "VALUES (:id, :k, :n, :s, true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
            ),
            {'id': str(uuid4()), 'k': key, 'n': name, 's': sort_order},
        )
    print(f"[c3b_device_categories] seeded {len(CATEGORIES)} system device_category row(s)")

    # --- 3. device_type: enum -> FK ---
    op.add_column('device_type', sa.Column('category_id', sa.Uuid(), nullable=True))
    backfilled_dt = connection.execute(text(
        "UPDATE device_type dt SET category_id = dc.id FROM device_category dc "
        "WHERE dc.key = dt.category::text AND dt.category_id IS NULL"
    )).rowcount
    print(f"[c3b_device_categories] backfilled device_type.category_id for {backfilled_dt} row(s)")
    orphans = connection.execute(text(
        "SELECT COUNT(*) FROM device_type WHERE category_id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(
            f"c3b_device_categories: {orphans} device_type row(s) have no matching "
            f"device_category — aborting (every enum label was seeded above; this "
            f"should be impossible)"
        )
    op.alter_column('device_type', 'category_id', nullable=False)
    op.create_foreign_key(
        'fk_device_type_category_id', 'device_type', 'device_category',
        ['category_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_index('ix_device_type_category_id', 'device_type', ['category_id'])
    op.drop_column('device_type', 'category')

    # --- 4. playbook: enum -> FK (stays nullable; RESTRICT — categories carry
    # referential meaning per doc 20a admin-categories-sidebar §1) ---
    op.add_column('playbook', sa.Column('target_category_id', sa.Uuid(), nullable=True))
    backfilled_pb = connection.execute(text(
        "UPDATE playbook p SET target_category_id = dc.id FROM device_category dc "
        "WHERE dc.key = p.target_category::text AND p.target_category IS NOT NULL "
        "AND p.target_category_id IS NULL"
    )).rowcount
    print(f"[c3b_device_categories] backfilled playbook.target_category_id for {backfilled_pb} row(s)")
    op.create_foreign_key(
        'fk_playbook_target_category_id', 'playbook', 'device_category',
        ['target_category_id'], ['id'], ondelete='RESTRICT',
    )
    op.drop_column('playbook', 'target_category')

    # --- 5. type drop — both users converted above, same-revision drop
    # (c2d_graph_removal precedent for networknodestatus). ---
    sa.Enum(name='devicecategory').drop(connection, checkfirst=True)
    print("[c3b_device_categories] upgrade complete")


def downgrade() -> None:
    """AMENDMENT 9 (doc 20-cycle3-design.md normative amendments #9): the
    guard keys on the FROZEN 13-LABEL ALLOWLIST, not `is_system` — future
    baseline categories added via isp_seed alone (a documented, no-migration
    path) are is_system=true but have no enum label, so an is_system-keyed
    guard would pass and then abort mid-downgrade on an invalid `::
    devicecategory` cast. Any REFERENCED category whose key is not one of the
    13 original enum labels makes downgrade impossible (custom super-admin
    categories cannot map back to a fixed enum) — raise before touching any
    schema.

    Data loss accepted and documented (matches c2d_graph_removal precedent):
    every unreferenced custom category, and every super-admin edit to a
    system row's name/sort_order/icon/is_active, is discarded when the table
    is dropped below — re-upgrade re-seeds the pristine 13. Prod loss is zero
    (whole c1-c3 chain ships as one release; prod never had this table).
    """
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    offending = connection.execute(text("""
        SELECT DISTINCT dc.key FROM device_category dc
        WHERE dc.key <> ALL(:labels)
          AND (
            EXISTS (SELECT 1 FROM device_type dt WHERE dt.category_id = dc.id)
            OR EXISTS (SELECT 1 FROM playbook p WHERE p.target_category_id = dc.id)
          )
    """), {"labels": _CATEGORY_LABELS}).fetchall()
    if offending:
        keys = sorted(r[0] for r in offending)
        raise RuntimeError(
            f"c3b_device_categories downgrade refused: referenced category key(s) "
            f"{keys} have no devicecategory enum label (not one of the original 13) "
            f"— repoint or delete these device_type/playbook rows before downgrading."
        )

    discarded_custom = connection.execute(text(
        "SELECT COUNT(*) FROM device_category WHERE key <> ALL(:labels)"
    ), {"labels": _CATEGORY_LABELS}).scalar()
    discarded_edits = connection.execute(text(
        "SELECT COUNT(*) FROM device_category WHERE key = ANY(:labels) "
        "AND (name <> ANY(:names) OR NOT is_active)"
    ), {"labels": _CATEGORY_LABELS, "names": [c[1] for c in CATEGORIES]}).scalar()
    print(f"[c3b_device_categories] downgrade: discarding {discarded_custom} unreferenced custom "
          f"categor{'y' if discarded_custom == 1 else 'ies'} and {discarded_edits} edited system "
          f"row(s) (name/is_active) — re-upgrade re-seeds the pristine 13")

    sa.Enum(*_CATEGORY_LABELS, name='devicecategory').create(connection, checkfirst=True)

    op.add_column(
        'device_type',
        sa.Column('category', PGEnum(*_CATEGORY_LABELS, name='devicecategory', create_type=False), nullable=True),
    )
    connection.execute(text(
        "UPDATE device_type dt SET category = dc.key::devicecategory "
        "FROM device_category dc WHERE dc.id = dt.category_id"
    ))
    op.alter_column('device_type', 'category', nullable=False)
    op.drop_constraint('fk_device_type_category_id', 'device_type', type_='foreignkey')
    op.drop_index('ix_device_type_category_id', table_name='device_type')
    op.drop_column('device_type', 'category_id')

    op.add_column(
        'playbook',
        sa.Column('target_category', PGEnum(*_CATEGORY_LABELS, name='devicecategory', create_type=False), nullable=True),
    )
    connection.execute(text(
        "UPDATE playbook p SET target_category = dc.key::devicecategory "
        "FROM device_category dc WHERE dc.id = p.target_category_id AND p.target_category_id IS NOT NULL"
    ))
    op.drop_constraint('fk_playbook_target_category_id', 'playbook', type_='foreignkey')
    op.drop_column('playbook', 'target_category_id')

    op.drop_table('device_category')
    print("[c3b_device_categories] downgrade complete")
