"""network_access.kind: rename 'olt' to 'outbound'

Revision ID: na1_kind_outbound
Revises: vpn1_vpn_socks5
Create Date: 2026-09-25

canon C9's non-ACS transport row was named `olt`, which was never what it
meant: it is the tenant's default OUTBOUND path — the row the transport
resolver reads to decide how Uplink dials ANY managed device, OLT or not.
The name misled every reader of transport.py, cli.py and worker.py, all three
of which carried their own copy of the `kind == 'olt' AND is_default` query.

**This revision narrows the CHECK to ('acs','outbound'). 'olt' stops being a
legal value.** An earlier draft did this additively (widen to
('acs','olt','outbound') now, narrow next cycle) because NetworkAccessOut
inherits NetworkAccessBase.validate_kind, so a backend still pinned to the old
models-utils would 500 on every `network_access` READ of a rewritten row
during the window between the models-utils push and the backend re-pin.

That window only matters if rows exist. Both the Railway `development` and the
production databases were checked directly before this revision was finalised:
`network_access` holds ZERO rows in both, and both sit at
`alembic_version = iv1_insights_v2` — i.e. neither has ever run nc1a's table in
anger. There is nothing to protect, so the additive dance was removed rather
than carried for a cycle. If that ever stops being true, the safe order is the
old one: widen, deploy, rewrite, narrow.

The `UPDATE ... WHERE kind = 'olt'` is kept even though it is a no-op on both
deployed databases — it is correct for any developer's local DB that does hold
an `olt` row, and the post-UPDATE assertion refuses to leave the rename
half-applied.

Order inside upgrade() is load-bearing: the old CHECK forbids 'outbound' and
the new one forbids 'olt', so the constraint is dropped, the rows are
rewritten, and only then is the narrow CHECK created.

`uq_network_access_default` (UNIQUE (company_id, kind) WHERE is_default) needs
no recreation — it indexes the kind COLUMN, not a value, and an in-place value
UPDATE preserves uniqueness (no 'outbound' row can pre-exist, the old CHECK
forbade it). nc1a_network_config_core.py:54's copy of the fragment is immutable
and correctly keeps ('acs','olt'): a fresh database migrates nc1a -> ... -> na1
and ends correct.

downgrade() reverses both halves (rows back to 'olt', CHECK back to the nc1a
pair).
"""
from alembic import op
import sqlalchemy as sa

revision = "na1_kind_outbound"
down_revision = "vpn1_vpn_socks5"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_vpn_transport_constants.py pins them equal.
_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','outbound')"
# nc1a's value, restored by downgrade().
_OLD_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','olt')"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    # Neither CHECK admits both spellings, so the rows are rewritten with no
    # constraint in place.
    op.drop_constraint("ck_network_access_kind", "network_access", type_="check")
    op.execute("UPDATE network_access SET kind = 'outbound' WHERE kind = 'olt'")
    leftover = connection.execute(
        sa.text("SELECT count(*) FROM network_access WHERE kind = 'olt'")
    ).scalar()
    if leftover:
        raise RuntimeError(
            f"[na1_kind_outbound] {leftover} network_access rows are still kind='olt' "
            "after the UPDATE — refusing to leave the rename half-applied"
        )
    op.create_check_constraint(
        "ck_network_access_kind", "network_access", sa.text(_NETWORK_ACCESS_KIND_CHECK)
    )
    print("[na1_kind_outbound] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))
    op.drop_constraint("ck_network_access_kind", "network_access", type_="check")
    op.execute("UPDATE network_access SET kind = 'olt' WHERE kind = 'outbound'")
    op.create_check_constraint(
        "ck_network_access_kind",
        "network_access",
        sa.text(_OLD_NETWORK_ACCESS_KIND_CHECK),
    )
    print("[na1_kind_outbound] downgrade complete")
