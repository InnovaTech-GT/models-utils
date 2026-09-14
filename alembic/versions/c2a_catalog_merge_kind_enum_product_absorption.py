"""cycle 2 entity merge R1: catalogkind enum, Product -> ServicePlan absorption, order_item.service_plan_id

Revision ID: c2a_catalog_merge
Revises: c1f_verify_grandfather
Create Date: 2026-07-06

Doc 18-cycle2-design.md D1/D2 + 18a-cycle2-design-appendix.md §1a, amendments
1/2. Entity merge NOW (founder decision D1): ServicePlan absorbs Product.
Additive DDL + backfill; physical drop of `product` is a LATER release
(destructive-after-prod rule) — this revision only bridges the two tables.

Style note (deviation from c1b/c1c's autocommit_block + batched-commit
pattern): prod ground truth (doc 17) is 14 products / 8,213 order_items —
two to three orders of magnitude smaller than a scenario needing incremental
per-batch commits. This revision runs entirely inside ONE transaction
(transaction_per_migration=True, set at the env.py level): any failure
(including a tripped end-assertion) rolls back the WHOLE revision atomically,
so a retry of `alembic upgrade head` starts from a clean slate — no
IF-NOT-EXISTS column guards are needed the way c1c needed them (its
autocommit_block commits DDL early, which this revision never does). The
backfill INSERT/UPDATE statements are still guarded (NOT EXISTS / IS NULL)
so the revision is also a no-op if the zero-data-loss rehearsal suite ever
re-executes it after a successful run (stepped-upgrade / downgrade /
re-upgrade drills, doc 18 §Test plan).

Amendments implemented here (supersede the appendix where they differ):
- migration_source is a DEDICATED column (service_plan.migration_source),
  never a JSON field — excluded from every Update schema (amendment 1).
- Pre-bridged products (a plan already had product_id set before this
  migration, e.g. via the old 'Billing Product' picker) are NOT skipped: the
  existing plan's `kind` is UPDATEd from the same name-based rule instead,
  and included in the printed mapping (amendment 2).
- Downgrade deletes ONLY migration_source='c2a' rows, and only when they have
  no client_service dependents (a suspension/generated order/etc. hanging off
  a bridged plan means real post-merge data exists — the row stays, per the
  zero-data-loss rollback-window rule).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c2a_catalog_merge'
down_revision: Union[str, Sequence[str], None] = 'c1f_verify_grandfather'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Name-based classification rules (appendix §1a) — validated against the 14
# known prod product names only; the printed mapping below is the artifact a
# human signs off on before promote (doc 18 designer risk).
_PLAN_TYPE_CASE = """
    CASE WHEN p.name ~* 'internet|mbps|fibra|fiber' THEN 'FIBER'
         WHEN p.name ~* 'cable|tv|hotel'            THEN 'CABLE'
         ELSE 'OTHER' END
"""
_KIND_CASE = """
    CASE WHEN p.name ~* 'instalacion|instalación|installation' THEN 'INSTALLATION'
         ELSE 'SERVICE' END
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- catalogkind enum (brand-new type — no ALTER TYPE ADD VALUE hazard) ---
    sa.Enum('SERVICE', 'INSTALLATION', 'HARDWARE', name='catalogkind').create(
        connection, checkfirst=True
    )

    # --- service_plan: kind / stock / migration_source ---
    op.add_column('service_plan', sa.Column(
        'kind', postgresql.ENUM(name='catalogkind', create_type=False),
        server_default='SERVICE', nullable=False,
    ))
    op.add_column('service_plan', sa.Column('stock', sa.Integer(), nullable=True))
    op.add_column('service_plan', sa.Column('migration_source', sa.String(), nullable=True))

    # Makes the Product->ServicePlan billing bridge deterministic (pre-assert:
    # no product_id duplicated among plans — a violation aborts index creation
    # loudly here rather than corrupting the backfill below).
    connection.execute(text(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_service_plan_product '
        'ON service_plan(product_id) WHERE product_id IS NOT NULL'
    ))

    # --- order_item.service_plan_id ---
    op.add_column('order_item', sa.Column('service_plan_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_order_item_service_plan', 'order_item', 'service_plan',
        ['service_plan_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_order_item_service_plan', 'order_item', ['service_plan_id'])

    # ------------------------------------------------------------------
    # Backfill: Product -> ServicePlan (one plan per product not already
    # bridged), migration_source='c2a' on inserted rows.
    # ------------------------------------------------------------------
    connection.execute(text(f"""
        INSERT INTO service_plan (
            id, created_at, updated_at, name, description, plan_type,
            download_mbps, upload_mbps, data_cap_gb, price, price_cents,
            is_active, kind, stock, migration_source, company_id, product_id
        )
        SELECT gen_random_uuid(), p.created_at, now(), p.name, NULLIF(p.description, ''),
               ({_PLAN_TYPE_CASE})::serviceplantype,
               NULL, NULL, NULL,
               p.price, p.price_cents, true,
               ({_KIND_CASE})::catalogkind,
               p.stock, 'c2a', p.company_id, p.id
        FROM product p
        WHERE NOT EXISTS (SELECT 1 FROM service_plan sp WHERE sp.product_id = p.id)
    """))

    # Amendment 2: pre-bridged products (a plan already pointed at this
    # product before c2a — e.g. an operator used the old 'Billing Product'
    # picker) get their kind UPDATEd from the same rule instead of being
    # skipped. Guarded by migration_source IS NULL so re-runs are no-ops and
    # this never touches c2a's own inserted rows.
    connection.execute(text(f"""
        UPDATE service_plan sp SET kind = ({_KIND_CASE})::catalogkind
        FROM product p
        WHERE sp.product_id = p.id AND sp.migration_source IS NULL
    """))

    # Full 14-row name->(plan_type,kind) resolution, printed for the
    # rehearsal report / founder sign-off (doc 18 designer risk, amendment 2).
    mapping = connection.execute(text(f"""
        SELECT p.name, ({_PLAN_TYPE_CASE}) AS plan_type, ({_KIND_CASE}) AS kind
        FROM product p ORDER BY p.name
    """)).fetchall()
    print(f"[c2a_catalog_merge] product name -> (plan_type, kind) resolution "
          f"({len(mapping)} rows):")
    for name, plan_type, kind in mapping:
        print(f"[c2a_catalog_merge]   {name!r} -> (plan_type={plan_type}, kind={kind})")

    # order_item.service_plan_id backfill from the product bridge (keep
    # product_id populated — rollback window dual-write).
    connection.execute(text("""
        UPDATE order_item oi SET service_plan_id = sp.id
        FROM service_plan sp
        WHERE sp.product_id = oi.product_id
          AND oi.product_id IS NOT NULL
          AND oi.service_plan_id IS NULL
    """))

    # ------------------------------------------------------------------
    # End assertions — RAISE on mismatch (migration fails loudly and rolls
    # back atomically; retry is `alembic upgrade head`).
    # ------------------------------------------------------------------
    failures = []

    unbridged_products = connection.execute(text("""
        SELECT COUNT(*) FROM product p
        WHERE NOT EXISTS (SELECT 1 FROM service_plan sp WHERE sp.product_id = p.id)
    """)).scalar()
    if unbridged_products:
        failures.append(f"products without a bridged plan: {unbridged_products}")

    unbridged_order_items = connection.execute(text("""
        SELECT COUNT(*) FROM order_item
        WHERE product_id IS NOT NULL AND service_plan_id IS NULL
    """)).scalar()
    if unbridged_order_items:
        failures.append(f"order_items with product_id but no service_plan_id: {unbridged_order_items}")

    if failures:
        raise RuntimeError(
            "c2a_catalog_merge post-conditions FAILED — " + "; ".join(failures)
        )

    # Informational only (not a hard gate): name collisions among plans per
    # company, for visibility in the rehearsal report.
    collisions = connection.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT company_id, name FROM service_plan GROUP BY company_id, name HAVING COUNT(*) > 1
        ) t
    """)).scalar()
    print(f"[c2a_catalog_merge] plan name collisions per company (informational): {collisions}")
    print("[c2a_catalog_merge] all post-condition assertions passed")


def downgrade() -> None:
    """Drop order_item.service_plan_id, delete ONLY migration_source='c2a'
    service_plan rows that have no dependents (a client_service referencing
    one means real post-merge billing data exists and the row must survive —
    c2b's downgrade reverse-materializes a recurring_order for exactly this
    case), then drop the added service_plan columns/index/enum.

    Amendment 1 (zero-data-loss): this is deliberately NOT a blanket delete
    of every product_id-bridged plan — a plan created by an operator via the
    old 'Billing Product' picker (migration_source IS NULL) is USER data and
    must never be destroyed by a rollback.
    """
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.drop_index('ix_order_item_service_plan', table_name='order_item')
    op.drop_constraint('fk_order_item_service_plan', 'order_item', type_='foreignkey')
    op.drop_column('order_item', 'service_plan_id')

    deleted = connection.execute(text("""
        DELETE FROM service_plan sp
        WHERE sp.migration_source = 'c2a'
          AND NOT EXISTS (SELECT 1 FROM client_service cs WHERE cs.service_plan_id = sp.id)
    """))
    print(f"[c2a_catalog_merge] downgrade: deleted {deleted.rowcount} migration-created plans "
          f"without dependents")
    survivors = connection.execute(text(
        "SELECT COUNT(*) FROM service_plan WHERE migration_source = 'c2a'"
    )).scalar()
    if survivors:
        print(f"[c2a_catalog_merge] downgrade: {survivors} migration-created plan(s) kept "
              f"(referenced by client_service — zero-data-loss)")

    connection.execute(text('DROP INDEX IF EXISTS uq_service_plan_product'))
    op.drop_column('service_plan', 'migration_source')
    op.drop_column('service_plan', 'stock')
    op.drop_column('service_plan', 'kind')

    sa.Enum(name='catalogkind').drop(connection, checkfirst=True)
