"""client.code: short per-company unique client id (legacy or random), overrideable

Revision ID: cc1_client_code
Revises: pt1_port_topology
Create Date: 2026-10-05

Additive. One VARCHAR(16) NOT NULL column with a DB-side random default, a
case-insensitive per-company unique index and a format CHECK.

Backfill: a client whose observations carry `[LEGACY_ID:<code>]` keeps that id
(uppercased) when it is valid and unique within its company; every other row
gets a random code from client_code_generate().

The DB default (client_code_generate) exists so writers that predate this
column — an old backend still serving during rollout, raw SQL, the ingest
scripts — insert a valid code without knowing about it. Its alphabet/length
are hand-kept in sync with database_utils.utils.client_code.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "cc1_client_code"
down_revision: Union[str, Sequence[str], None] = "pt1_port_topology"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "ck_client_code_format"
INDEX = "uq_client_company_code"

GENERATE_FN = """
CREATE OR REPLACE FUNCTION client_code_generate() RETURNS varchar
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
  alphabet constant text := '23456789ABCDEFGHJKMNPQRSTUVWXYZ';
  result text := '';
BEGIN
  FOR i IN 1..6 LOOP
    result := result || substr(alphabet, 1 + floor(random() * length(alphabet))::int, 1);
  END LOOP;
  RETURN result;
END $$;
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute(GENERATE_FN)
    op.execute("ALTER TABLE client ADD COLUMN IF NOT EXISTS code VARCHAR(16)")

    # Legacy ids first: only tags that are well-formed and unique within the
    # company. Anything ambiguous falls through to a random code.
    op.execute(r"""
        WITH tagged AS (
          SELECT id, company_id,
                 upper(substring(observations FROM '\[LEGACY_ID:([^]]+)\]')) AS legacy
          FROM client
          WHERE code IS NULL AND observations ~ '\[LEGACY_ID:[^]]+\]'
        ), usable AS (
          SELECT t.id, t.legacy FROM tagged t
          WHERE t.legacy ~ '^[A-Z0-9-]{1,16}$'
            AND (SELECT count(*) FROM tagged u
                 WHERE u.company_id = t.company_id AND u.legacy = t.legacy) = 1
        )
        UPDATE client c SET code = usable.legacy FROM usable WHERE c.id = usable.id
    """)
    # Everyone else: random, re-rolled until unique within the company.
    op.execute("""
        DO $$
        DECLARE r record; candidate varchar;
        BEGIN
          FOR r IN SELECT id, company_id FROM client WHERE code IS NULL LOOP
            LOOP
              candidate := client_code_generate();
              EXIT WHEN NOT EXISTS (
                SELECT 1 FROM client WHERE company_id = r.company_id AND upper(code) = candidate);
            END LOOP;
            UPDATE client SET code = candidate WHERE id = r.id;
          END LOOP;
        END $$;
    """)

    op.execute("ALTER TABLE client ALTER COLUMN code SET DEFAULT client_code_generate()")
    op.execute("ALTER TABLE client ALTER COLUMN code SET NOT NULL")
    if connection.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": CHECK}).scalar() is None:
        op.execute(f"ALTER TABLE client ADD CONSTRAINT {CHECK} CHECK (code ~ '^[A-Z0-9-]{{1,16}}$')")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON client (company_id, upper(code))")

    missing = connection.execute(text("SELECT count(*) FROM client WHERE code IS NULL")).scalar()
    if missing:
        raise RuntimeError(f"[cc1] {missing} clients without a code after upgrade")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE client DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute("ALTER TABLE client DROP COLUMN IF EXISTS code")
    op.execute("DROP FUNCTION IF EXISTS client_code_generate()")
