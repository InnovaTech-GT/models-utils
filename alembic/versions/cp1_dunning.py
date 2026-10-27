"""cp1_dunning: dunning settings, payment promises, dunning runs, portal RBAC

Revision ID: cp1_dunning
Revises: wb1_whatsapp_bot_config
Create Date: 2026-10-26

Doc 52 (captive portal + automatic suspension), phase 1. Additive:
  - company.dunning_settings JSON NULL (validated by schemas.company.DunningSettings)
  - payment_promise (per service; one OPEN per client_service_id)
  - dunning_run (one row per tenant per daily run)
  - permission captive_portal.access + global CAPTIVE_PORTAL role holding it.
    ADMIN converges via the seed pass (env.py); never granted to humans.

PERMISSION / ROLE are pinned against the seeds by tests/test_cp1_dunning.py.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "cp1_dunning"
down_revision: Union[str, Sequence[str], None] = "wb1_whatsapp_bot_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSION = {
    "name": "captive_portal.access", "resource": "captive_portal", "action": "access",
    "description": "Portal cautivo: consultar la deuda de un servicio suspendido y registrar promesas de pago",
}
ROLE = {
    "name": "CAPTIVE_PORTAL",
    "description": "Captive portal service user: view a suspended service's debt, record payment promises",
}


def _enum(c, name: str, labels: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{label}'" for label in labels)
    c.execute(text(
        f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({quoted}); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    ))


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))

    c.execute(text("ALTER TABLE company ADD COLUMN IF NOT EXISTS dunning_settings JSON"))

    _enum(c, "paymentpromisesource", ("PORTAL", "COLLECTOR", "BOT"))
    _enum(c, "paymentpromisestatus", ("OPEN", "KEPT", "BROKEN", "CANCELLED"))
    _enum(c, "dunningrunoutcome", ("OK", "TRIPPED"))

    c.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_promise (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            promised_date DATE NOT NULL,
            source paymentpromisesource NOT NULL,
            status paymentpromisestatus NOT NULL DEFAULT 'OPEN',
            reconnected BOOLEAN NOT NULL DEFAULT false,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            client_id UUID NOT NULL REFERENCES client(id) ON DELETE CASCADE,
            client_service_id UUID NOT NULL REFERENCES client_service(id) ON DELETE CASCADE,
            suspension_id UUID REFERENCES service_suspension(id) ON DELETE SET NULL,
            created_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL
        )
    """))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_promise_open_service "
        "ON payment_promise (client_service_id) WHERE status = 'OPEN'"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_payment_promise_open_date "
        "ON payment_promise (company_id, promised_date) WHERE status = 'OPEN'"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_payment_promise_service ON payment_promise (client_service_id)"
    ))

    c.execute(text("""
        CREATE TABLE IF NOT EXISTS dunning_run (
            id UUID PRIMARY KEY,
            ran_at TIMESTAMPTZ NOT NULL,
            mode VARCHAR(10) NOT NULL,
            outcome dunningrunoutcome NOT NULL,
            candidates JSON NOT NULL,
            suspended JSON NOT NULL,
            skipped JSON NOT NULL,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            CONSTRAINT ck_dunning_run_mode CHECK (mode IN ('REPORT', 'ON'))
        )
    """))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_dunning_run_company_ran_at ON dunning_run (company_id, ran_at)"
    ))

    c.execute(text(
        "INSERT INTO permission (id, created_at, name, resource, action, description) "
        "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
        "ON CONFLICT (name) DO NOTHING"
    ), PERMISSION)
    c.execute(text(
        "INSERT INTO role (id, created_at, name, description, is_system) "
        "SELECT gen_random_uuid(), NOW(), :name, :description, TRUE "
        "WHERE NOT EXISTS (SELECT 1 FROM role WHERE name = :name AND company_id IS NULL)"
    ), ROLE)
    c.execute(text(
        "INSERT INTO role_permission (role_id, permission_id) "
        "SELECT r.id, p.id FROM role r, permission p "
        "WHERE r.name = :role AND r.company_id IS NULL AND p.name = :perm "
        "ON CONFLICT DO NOTHING"
    ), {"role": ROLE["name"], "perm": PERMISSION["name"]})


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))

    # Same caveat as cfg3: env.py's seed pass re-inserts the permission/role
    # after any online command; revert the seed edits to remove them for good.
    c.execute(text(
        "DELETE FROM role_permission WHERE permission_id IN "
        "(SELECT id FROM permission WHERE name = :perm) OR role_id IN "
        "(SELECT id FROM role WHERE name = :role AND company_id IS NULL)"
    ), {"perm": PERMISSION["name"], "role": ROLE["name"]})
    c.execute(text(
        "DELETE FROM user_role WHERE role_id IN "
        "(SELECT id FROM role WHERE name = :role AND company_id IS NULL)"
    ), {"role": ROLE["name"]})
    c.execute(text("DELETE FROM role WHERE name = :role AND company_id IS NULL"), {"role": ROLE["name"]})
    c.execute(text("DELETE FROM permission WHERE name = :perm"), {"perm": PERMISSION["name"]})

    c.execute(text("DROP TABLE IF EXISTS dunning_run"))
    c.execute(text("DROP TABLE IF EXISTS payment_promise"))
    c.execute(text("DROP TYPE IF EXISTS dunningrunoutcome"))
    c.execute(text("DROP TYPE IF EXISTS paymentpromisestatus"))
    c.execute(text("DROP TYPE IF EXISTS paymentpromisesource"))
    c.execute(text("ALTER TABLE company DROP COLUMN IF EXISTS dunning_settings"))
