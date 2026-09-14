"""
Tier Seed Script - Automatically seeds subscription tiers

This script is automatically run after each Alembic migration to ensure
tier data exists in the database.

The script is idempotent - it checks for existing data before inserting,
so it's safe to run multiple times.
"""
import json
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

# Full module set (sidebar/permissions gating). Free/Trial get everything
# while the "no limits on free tier" product decision stands.
ALL_MODULES = ["core", "admin", "management", "automations",
               "inventory", "topologies", "provisioning"]

# Network modules every paid tier gets (t2_paid_tier_network_modules).
NETWORK_MODULES = ["inventory", "topologies", "provisioning"]
PAID_TIER_NAMES = ('Basic', 'Premium', 'Pro', 'Enterprise')


def _enforce_free_trial_unlimited(connection: Connection) -> None:
    """Converge the standing product policy: Free/Trial have NO resource
    limits and every module (see revision t1_free_trial_unlimited).

    Runs on every migrate — this makes the policy survive prod-data reloads
    (scripts/load-prod-data.sh restores prod tier rows, which would otherwise
    silently revert the t1 data migration). Idempotent by construction.
    Remove this call (with a revision) when real Free/Trial limits return.
    """
    modules_json = json.dumps(ALL_MODULES)
    result = connection.execute(
        text(
            "UPDATE tier "
            "SET features = COALESCE(features::jsonb, '{}'::jsonb) "
            "    || '{\"max_users\": -1, \"max_products\": -1, \"max_clients\": -1}'::jsonb, "
            "    modules = :modules "
            "WHERE name IN ('Free', 'Trial') "
            "AND (features->>'max_users' IS DISTINCT FROM '-1' "
            "     OR features->>'max_products' IS DISTINCT FROM '-1' "
            "     OR features->>'max_clients' IS DISTINCT FROM '-1' "
            "     OR modules IS NULL OR modules::jsonb <> (:modules)::jsonb)"
        ),
        {"modules": modules_json},
    )
    connection.commit()
    if result.rowcount:
        logger.info(f"✓ Converged {result.rowcount} tier(s) to the Free/Trial-unlimited policy")


def _enforce_paid_tier_network_modules(connection: Connection) -> None:
    """Converge the standing product policy: every paid tier includes the
    Network modules (see revision t2_paid_tier_network_modules).

    Unions NETWORK_MODULES into whatever the tier already has, rather than
    overwriting — preserves any other module customization on the row.
    Runs on every migrate so this survives prod-data reloads. Idempotent.
    """
    result = connection.execute(
        text(
            "UPDATE tier "
            "SET modules = ("
            "    SELECT COALESCE(jsonb_agg(DISTINCT m), '[]'::jsonb) "
            "    FROM jsonb_array_elements_text("
            "        COALESCE(modules::jsonb, '[]'::jsonb) || CAST(:network_modules AS jsonb)"
            "    ) AS m"
            ") "
            "WHERE name = ANY(:tier_names) "
            "AND NOT (COALESCE(modules::jsonb, '[]'::jsonb) @> CAST(:network_modules AS jsonb))"
        ),
        {"network_modules": json.dumps(NETWORK_MODULES), "tier_names": list(PAID_TIER_NAMES)},
    )
    connection.commit()
    if result.rowcount:
        logger.info(f"✓ Converged {result.rowcount} paid tier(s) to include Network modules")


def seed_tier_data(connection: Connection) -> None:
    """
    Seed subscription tiers into the database.

    This function is idempotent - it checks if data exists before inserting.
    Safe to run multiple times.

    Args:
        connection: SQLAlchemy connection object
    """
    try:
        # Check if tier table exists
        table_exists = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'tier'
                """
            )
        ).scalar()

        if not table_exists:
            logger.info("Tier table does not exist yet. Skipping seed.")
            return

        # Check if tiers already exist
        existing_tiers = connection.execute(
            text("SELECT COUNT(*) FROM tier")
        ).scalar()

        # Check if existing tiers have billing data (non-zero prices or stripe_price_id)
        tiers_with_billing = connection.execute(
            text("SELECT COUNT(*) FROM tier WHERE price > 0 OR stripe_price_id IS NOT NULL")
        ).scalar()

        if existing_tiers > 0 and tiers_with_billing > 0:
            logger.info(f"Tier data already seeded ({existing_tiers} tiers with billing data found). Skipping.")
            _enforce_free_trial_unlimited(connection)
            _enforce_paid_tier_network_modules(connection)
            return

        # Define tier data mapping (name -> data)
        tiers_data_map = {
            # Free/Trial are currently UNLIMITED (all modules, no resource
            # caps) by product decision — see revision t1_free_trial_unlimited.
            "Free": {
                "price": 0,  # $0.00
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": -1,
                    "max_products": -1,
                    "max_clients": -1,
                    "support": "Community",
                    "features": ["Basic CRM", "Dashboard", "Reports"]
                },
                "modules": ALL_MODULES,
                "stripe_price_id": None,
                # Recurrente paywall: no free tier. Seeded inactive so a fresh/wiped
                # DB (empty tier table -> insert path below) can't resurrect it as
                # assignable, mirroring alembic/versions/6e7506e57be9 for DBs that
                # already had rows. See docs/billing.md (auth-erp).
                "is_active": False
            },
            "Trial": {
                "price": 0,  # $0.00
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": -1,
                    "max_products": -1,
                    "max_clients": -1,
                    "support": "Email",
                    "trial_days": 14,
                    "features": ["Full CRM", "Dashboard", "Advanced Reports", "API Access"]
                },
                "modules": ALL_MODULES,
                "stripe_price_id": None,
                # Same as Free above — no trial tier anymore.
                "is_active": False
            },
            "Basic": {
                "price": 1999,  # $19.99
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": 10,
                    "max_products": 200,
                    "max_clients": 500,
                    "support": "Email",
                    "features": ["Full CRM", "Dashboard", "Advanced Reports", "API Access", "Integrations"]
                },
                "modules": ["core", "admin", "management", "automations"] + NETWORK_MODULES,
                "stripe_price_id": "price_basic_monthly",  # Mock Stripe price ID
                "is_active": True
            },
            "Premium": {
                "price": 9999,  # $99.99
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": -1,  # Unlimited
                    "max_products": -1,  # Unlimited
                    "max_clients": -1,  # Unlimited
                    "support": "Priority",
                    "features": [
                        "Full CRM",
                        "Dashboard",
                        "Advanced Reports",
                        "API Access",
                        "Integrations",
                        "Custom Workflows",
                        "Dedicated Account Manager",
                        "SLA Guarantee"
                    ]
                },
                "modules": ["core", "admin", "management", "automations"] + NETWORK_MODULES,
                "stripe_price_id": "price_premium_monthly",  # Mock Stripe price ID
                "is_active": True
            },
            # Handle old tier names
            "Pro": {  # Map to Premium
                "price": 9999,
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": -1,
                    "max_products": -1,
                    "max_clients": -1,
                    "support": "Priority",
                    "features": ["Full CRM", "Dashboard", "Advanced Reports", "API Access", "Integrations", "Custom Workflows"]
                },
                "modules": ["core", "admin", "management", "automations"] + NETWORK_MODULES,
                "stripe_price_id": "price_premium_monthly",
                "is_active": True
            },
            "Enterprise": {  # Keep Enterprise tier
                "price": 19999,  # $199.99
                "billing_cycle": "MONTHLY",
                "features": {
                    "max_users": -1,
                    "max_products": -1,
                    "max_clients": -1,
                    "support": "Dedicated",
                    "features": ["Full CRM", "Dashboard", "Advanced Reports", "API Access", "Integrations", "Custom Workflows", "White Label", "SLA Guarantee"]
                },
                "modules": ["core", "admin", "management", "automations"] + NETWORK_MODULES,
                "stripe_price_id": "price_enterprise_monthly",
                "is_active": True
            }
        }

        # If tiers exist but have no billing data, update them
        if existing_tiers > 0:
            logger.info(f"Found {existing_tiers} tiers with default billing data. Updating with proper data...")

            # Get existing tiers
            existing_tier_rows = connection.execute(
                text("SELECT id, name FROM tier")
            ).fetchall()

            updated_count = 0
            for tier_row in existing_tier_rows:
                tier_id = tier_row[0]
                tier_name = tier_row[1]

                if tier_name in tiers_data_map:
                    tier_data = tiers_data_map[tier_name]
                    result = connection.execute(
                        text(
                            "UPDATE tier SET "
                            "price = :price, "
                            "billing_cycle = :billing_cycle, "
                            "features = :features, "
                            "stripe_price_id = :stripe_price_id, "
                            "is_active = :is_active "
                            "WHERE id = :tier_id "
                            # Per-row guard: only bootstrap rows that have NO
                            # billing data yet. Without it, any state where no
                            # tier has price>0 (e.g. a future pricing change)
                            # would bulk-overwrite customized price/features/
                            # is_active with these hardcoded values on every
                            # migrate until some tier regains a price.
                            "AND (price IS NULL OR price = 0) "
                            "AND stripe_price_id IS NULL"
                        ),
                        {
                            'tier_id': tier_id,
                            'price': tier_data['price'],
                            'billing_cycle': tier_data['billing_cycle'],
                            'features': json.dumps(tier_data['features']),
                            'stripe_price_id': tier_data['stripe_price_id'],
                            'is_active': tier_data['is_active']
                        }
                    )
                    updated_count += result.rowcount
                else:
                    logger.warning(f"Unknown tier '{tier_name}' found - leaving unchanged")

            connection.commit()
            logger.info(f"✓ Updated {updated_count} existing tiers with billing data")
            return

        logger.info("Seeding subscription tiers...")

        # Insert new tiers (only if table was empty)
        tiers_to_insert = ["Free", "Trial", "Basic", "Premium"]
        for tier_name in tiers_to_insert:
            tier_data = tiers_data_map[tier_name]
            connection.execute(
                text(
                    "INSERT INTO tier (id, created_at, name, price, billing_cycle, features, modules, stripe_price_id, is_active) "
                    "VALUES (gen_random_uuid(), :created_at, :name, :price, :billing_cycle, :features, :modules, :stripe_price_id, :is_active)"
                ),
                {
                    'created_at': now_gt(),
                    'name': tier_name,
                    'price': tier_data['price'],
                    'billing_cycle': tier_data['billing_cycle'],
                    'features': json.dumps(tier_data['features']),
                    'modules': json.dumps(tier_data['modules']) if tier_data.get('modules') else None,
                    'stripe_price_id': tier_data['stripe_price_id'],
                    'is_active': tier_data['is_active']
                }
            )

        connection.commit()
        logger.info(f"✓ Seeded {len(tiers_to_insert)} subscription tiers")
        logger.info("✓ Tier seed completed successfully!")

    except Exception as e:
        logger.error(f"Error seeding tier data: {e}")
        raise
