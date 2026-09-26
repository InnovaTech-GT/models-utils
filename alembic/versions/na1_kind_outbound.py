"""network_access.kind: rename 'olt' to 'outbound' (ADDITIVE)

Revision ID: na1_kind_outbound
Revises: vpn1_vpn_socks5
Create Date: 2026-09-25

canon C9's non-ACS transport row was named `olt`, which was never what it
meant: it is the tenant's default OUTBOUND path — the row the transport
resolver reads to decide how Uplink dials ANY managed device, OLT or not.
The name misled every reader of transport.py, cli.py and worker.py, all three
of which carried their own copy of the `kind == 'olt' AND is_default` query.

**This revision is ADDITIVE, on purpose.** The CHECK is WIDENED to
('acs','olt','outbound') and the rows are rewritten to 'outbound'; 'olt' stays
a legal value. Narrowing it to ('acs','outbound') is a trailing no-data
revision NEXT cycle, once every service is in production on the tolerant read
path.

Why it cannot be narrowed here: NetworkAccessOut inherits
NetworkAccessBase.validate_kind (database_utils/schemas/network_access.py), so
any service whose NETWORK_ACCESS_KINDS lacks a value raises on every
`network_access` READ of a row carrying it. models-utils migrates FIRST and the
backends are re-pinned afterwards (workspace CLAUDE.md, models-utils-first
push order), which is mandatory for the additive columns in vpn1/ac1 — the new
ORM SELECTs them and would otherwise hit UndefinedColumn. So between the
migrate job and the backend redeploy there is a window in which the deployed
backend holds the OLD kind set. A narrowing CHECK plus a rewritten row makes
`GET /network-access/` and `GET /network/settings` 500 for the whole tenant in
that window: destructive, and CLAUDE.md pitfall 6 verbatim ("removing or
renaming: all consuming service code must be in production FIRST"). Those two
ordering requirements are mutually exclusive. Widening removes the conflict.

Order inside upgrade() is load-bearing: widen the CHECK BEFORE the UPDATE, or
the old CHECK rejects the new value.

`uq_network_access_default` (UNIQUE (company_id, kind) WHERE is_default) needs
no recreation — it indexes the kind COLUMN, not a value, and an in-place value
UPDATE preserves uniqueness (no 'outbound' row can pre-exist, the old CHECK
forbade it). nc1a_network_config_core.py:54's copy of the fragment is
immutable and correctly keeps ('acs','olt'): a fresh database migrates
nc1a -> ... -> na1 and ends correct.

downgrade() reverses both halves (rows back to 'olt', CHECK back to the nc1a
pair), which is only possible BECAUSE this revision never narrowed anything.
"""
from alembic import op
import sqlalchemy as sa

revision = "na1_kind_outbound"
down_revision = "vpn1_vpn_socks5"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_vpn_transport_constants.py pins them equal.
_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','olt','outbound')"
# nc1a's value, restored by downgrade().
_OLD_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','olt')"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    # Widen first — the old CHECK forbids the new value.
    op.drop_constraint("ck_network_access_kind", "network_access", type_="check")
    op.create_check_constraint(
        "ck_network_access_kind", "network_access", sa.text(_NETWORK_ACCESS_KIND_CHECK)
    )
    op.execute("UPDATE network_access SET kind = 'outbound' WHERE kind = 'olt'")
    leftover = connection.execute(
        sa.text("SELECT count(*) FROM network_access WHERE kind = 'olt'")
    ).scalar()
    if leftover:
        raise RuntimeError(
            f"[na1_kind_outbound] {leftover} network_access rows are still kind='olt' "
            "after the UPDATE — refusing to leave the rename half-applied"
        )
    print("[na1_kind_outbound] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.execute("UPDATE network_access SET kind = 'olt' WHERE kind = 'outbound'")
    op.drop_constraint("ck_network_access_kind", "network_access", type_="check")
    op.create_check_constraint(
        "ck_network_access_kind",
        "network_access",
        sa.text(_OLD_NETWORK_ACCESS_KIND_CHECK),
    )
    print("[na1_kind_outbound] downgrade complete")
