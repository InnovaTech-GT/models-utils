"""brownfield adoption (attested installs): client_service adoption columns + client_services.adopt permission

Revision ID: ba1_attested_adoption
Revises: t2_grandfather_email_verified
Create Date: 2026-07-19

Doc 30 (docs/isp-platform/30-brownfield-adoption-design.md). Additive DDL for
attested adoption — a persistent ATTESTATION FACT that substitutes for the
missing SUCCEEDED activation job when deriving install_state for services
installed before Uplink:

  - client_service.adopted_at TIMESTAMPTZ NULL — when the attestation was made.
  - client_service.adopted_by_user_id UUID NULL, FK
    fk_client_service_adopted_by_user -> "user"(id) ON DELETE SET NULL
    (deleting the attesting user keeps the attestation fact; only authorship
    is lost — ServiceSuspension.created_by precedent).
  - client_service.adoption_note VARCHAR NULL — required provenance note.
  - Partial index ix_client_service_adopted ON client_service (company_id)
    WHERE adopted_at IS NOT NULL (adoption-campaign scans; adopted rows are a
    small minority forever).
  - Permission client_services.adopt (idempotent INSERT) granted to the
    global (company_id IS NULL) system ADMIN role ONLY.

Deliberately NOT changed: ck_client_service_install_state and INSTALL_STATES
— adoption adds NO new state. The install_state derivation change (adopted_at
as a fallback inside _activation_ok, real job evidence checked FIRST) is
backend-erp code, not DDL. The permission is deliberately ADMIN-only: MANAGER
exclusion is enforced by the seeds shipped in this same commit
(rbac_seed.MANAGER_EXCLUDED_PERMISSIONS / isp_seed.ADMIN_ONLY_PERMISSIONS);
no ISP base role receives it and rbac_seed step 4 has no grant-copy source
for it. This revision also satisfies the 'every seed change ships with a
revision' rule (migrate.yml is path-filtered on alembic/**).

Hand-written (NOT autogenerate), nc2a house style: guarded/idempotent ops,
post-upgrade assertions, total downgrade. Downgrade drops exactly what this
revision created — the adoption columns drop WITH their data, so downgrading
destroys attestation facts (consistent with house downgrade-totality).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'ba1_attested_adoption'
down_revision: Union[str, Sequence[str], None] = 't2_grandfather_email_verified'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against alembic/seeds/isp_seed.py by tests/test_attested_adoption.py
# (revisions are immutable, seeds are not — neither can import the other).
ADOPT_PERMISSION = {
    "name": "client_services.adopt",
    "resource": "client_services",
    "action": "adopt",
    "description": "Attest a service as installed (brownfield adoption) - ADMIN only",
}

_NEW_COLUMNS = (
    ("client_service", "adopted_at"),
    ("client_service", "adopted_by_user_id"),
    ("client_service", "adoption_note"),
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- 1. additive columns (IF NOT EXISTS = re-run safe) ---
    op.execute(
        "ALTER TABLE client_service ADD COLUMN IF NOT EXISTS adopted_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE client_service ADD COLUMN IF NOT EXISTS adopted_by_user_id UUID")
    op.execute("ALTER TABLE client_service ADD COLUMN IF NOT EXISTS adoption_note VARCHAR")

    # --- 2. attester FK (DROP IF EXISTS + ADD = re-run safe). SET NULL:
    # deleting the attesting user must never erase the attestation fact.
    # "user" is double-quoted — reserved word (t2 precedent). ---
    op.execute(
        "ALTER TABLE client_service DROP CONSTRAINT IF EXISTS fk_client_service_adopted_by_user"
    )
    op.execute(
        "ALTER TABLE client_service "
        "ADD CONSTRAINT fk_client_service_adopted_by_user "
        'FOREIGN KEY (adopted_by_user_id) REFERENCES "user" (id) ON DELETE SET NULL'
    )

    # --- 3. adoption-campaign partial index (doc 30) ---
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_client_service_adopted "
        "ON client_service (company_id) WHERE adopted_at IS NOT NULL"
    )

    # --- 4. permission insert (9e6f5200dac4 precedent, idempotent) ---
    connection.execute(
        text(
            "INSERT INTO permission (id, created_at, name, resource, action, description) "
            "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        ADOPT_PERMISSION,
    )

    # --- 5. grant to the global system ADMIN ONLY (payments.refund policy:
    # NOT MANAGER, NOT any ISP base role — the seeds enforce the exclusion) ---
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r, permission p "
            "WHERE r.name = 'ADMIN' AND r.company_id IS NULL "
            "AND p.name = 'client_services.adopt' "
            "ON CONFLICT DO NOTHING"
        )
    )

    # --- 6. assertions: every object landed ---
    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[ba1] expected column(s) missing after upgrade: {missing}")
    fk = connection.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = 'fk_client_service_adopted_by_user'"
    )).fetchone()
    if not fk:
        raise RuntimeError(
            "[ba1] expected FK 'fk_client_service_adopted_by_user' to exist after upgrade"
        )
    idx = connection.execute(
        text("SELECT to_regclass('ix_client_service_adopted')")
    ).scalar()
    if idx is None:
        raise RuntimeError(
            "[ba1] expected index 'ix_client_service_adopted' to exist after upgrade"
        )
    perm = connection.execute(text(
        "SELECT 1 FROM permission WHERE name = 'client_services.adopt'"
    )).fetchone()
    if not perm:
        raise RuntimeError(
            "[ba1] expected permission 'client_services.adopt' to exist after upgrade"
        )

    print("[ba1_attested_adoption] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    connection.execute(text(
        "DELETE FROM role_permission WHERE permission_id = "
        "(SELECT id FROM permission WHERE name = 'client_services.adopt')"
    ))
    connection.execute(text("DELETE FROM permission WHERE name = 'client_services.adopt'"))

    op.execute("DROP INDEX IF EXISTS ix_client_service_adopted")
    op.execute(
        "ALTER TABLE client_service DROP CONSTRAINT IF EXISTS fk_client_service_adopted_by_user"
    )
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS adoption_note")
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS adopted_by_user_id")
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS adopted_at")

    print("[ba1_attested_adoption] downgrade complete")
