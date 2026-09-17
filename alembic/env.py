import os
import sys
from logging.config import fileConfig
from dotenv import load_dotenv

from sqlalchemy import engine_from_config, pool
from alembic import context

# --- 1. Adjust path for imports ---
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Add alembic directory to path for seed imports
sys.path.insert(0, os.path.dirname(__file__))

# --- 2. Load .env file and set DB URL from env ---
load_dotenv()
config = context.config
db_url = os.getenv("DB_URL")
if db_url:
    config.set_main_option('sqlalchemy.url', db_url)

# --- 3. Set up logging ---
if config.config_file_name:
    fileConfig(config.config_file_name)

# --- 4. Import models ---
from database_utils import database
import database_utils.models.auth
import database_utils.models.crm
import database_utils.models.workflow
import database_utils.models.isp

# --- 5. Target metadata ---
target_metadata = database.Base.metadata

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Each revision commits in its own transaction (doc 16 §2.0, BLOCKER fix):
            # - a failed revision no longer rolls back previously applied ones
            #   (documented retry-from-Rn procedure becomes valid),
            # - ADD-COLUMN locks are released before long data backfills,
            # - NOT VALID -> VALIDATE and using enum values added by an earlier
            #   revision become possible.
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    # --- Seeds: run AFTER all migrations, in their own transaction ---
    # (Previously they ran inside the single migration transaction: one seed
    # failure rolled back every revision. Doc 16 §2.0.)
    with connectable.connect() as connection:
        _run_seeds(connection)


def _run_seeds(connection) -> None:
    """Seed RBAC / tier / ISP data. Idempotent; called after every upgrade."""
    # Automatically seed RBAC data after migrations (commits internally)
    from seeds.rbac_seed import seed_rbac_data
    seed_rbac_data(connection)

    # Automatically seed tier data after migrations (commits internally)
    from seeds.tier_seed import seed_tier_data
    seed_tier_data(connection)

    # Automatically seed ISP data (permissions, roles, tier modules,
    # workflow templates) — idempotent; skipped until the isp-platform
    # revision has created its tables.
    #
    # Cycle 2 (doc 18 amendment 10): the sentinel used to require BOTH
    # workflow_template AND network_node_type to exist. c2d_graph_removal
    # drops network_node_type entirely, which would make this permanently
    # false on any DB migrated past c2d — the ISP seed (permissions, roles,
    # tier modules, templates, the new client_services.generate permission)
    # would silently stop converging forever. workflow_template alone is
    # sufficient: it is created by cd2f and never dropped. Table-existence
    # guards for individual sub-seeders that touch tables removed later in
    # the chain (e.g. network_node_type) now live inside isp_seed.py itself.
    from sqlalchemy import text as _text
    isp_tables = connection.execute(_text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'workflow_template'"
    )).scalar()
    if isp_tables == 1:
        from seeds.isp_seed import seed_isp_data
        seed_isp_data(connection)
        # isp_seed does not commit internally (rbac/tier seeds do).
        connection.commit()

        # Final convergence pass: isp_seed may have just created roles
        # (BILLING, SUPPORT, TECHNICIAN, ...) AFTER the RBAC reconcile ran,
        # so derived grants (orders.* -> payments.*) would otherwise only
        # land on the NEXT migrate. Re-reconcile so a single run converges.
        from seeds.rbac_seed import _ensure_convergent_rbac
        _ensure_convergent_rbac(connection)
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
