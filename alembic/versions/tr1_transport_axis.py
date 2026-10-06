"""transport axis: fold network_access into provisioning_settings, drop the table

Revision ID: tr1_transport_axis
Revises: ac1_acs_tenant_auth
Create Date: 2026-09-26

`network_access` conflated two unrelated things behind a `kind` discriminator
(`acs` = a settings bucket, `outbound` = a transport) and modelled a tenant-wide,
mutually exclusive choice as a MULTI-ROW table. Multi-row existed only to serve
per-CIDR longest-prefix resolution via `mgmt_subnets`, which was never
implemented and is now abandoned outright. And `mode` enumerated the CROSS
PRODUCT of two independent questions, which is why "ZeroTier + managed routes"
had no value available at all.

This revision replaces `mode` with two orthogonal fields on the tenant
singleton `provisioning_settings` (canon C6, `company_id` UNIQUE):

    dial_target   'device' | 'gateway'   -- whose address do we dial
    proxy_kind    'none'   | 'socks5'    -- is there a hop, and of what sort

plus their operands `gateway_host` and `proxy_address`. That covers all five
operator scenarios and any future SDN (Tailscale, Nebula, canon C10's edge
agent as `proxy_kind='agent'`) with no schema change:

    device  + none    <- old 'direct'      devices have public IPs
    gateway + none    <- old 'nat_public'  NAT + port map to a public IP
    gateway + socks5  <- old 'nat_zt'      NAT + port map via ZeroTier
    device  + socks5  <- old 'vpn'         WireGuard hub + managed routes
    device  + socks5  <- (did not exist)   ZeroTier + managed routes

`proxy_kind` is LOAD-BEARING and deliberately NOT collapsed into
`proxy_address IS NOT NULL`: `device` + no proxy is the legitimate public-IP
case, so without an explicit intent column the resolver cannot tell "no proxy
needed" from "a hub is intended but its address is missing" — and the second
would silently dial an RFC1918 address from the Railway container. That is the
canon R23 fail-closed guarantee the old mode values existed to provide.

Also moved off `network_access` onto the same row: `acs_base_url` (informational
only — nothing in code reads it; its job is telling an installer what to type)
and `acs_auth_required` (the Capa 3 gate, decision 8, unchanged semantics —
NOT NULL, default false, and false means ALLOW).

And the tenant TR-069 credential becomes TWO explicit FKs,
`cwmp_credential_id` / `cwmp_pending_credential_id`, replacing
`device_credential.network_access_id` plus the "newest vs second-newest
HTTP_BASIC row bound to the acs row" inference the accept-both rotation window
used to rest on. One credential per tenant is now true by construction.

ORDER INSIDE upgrade() IS LOAD-BEARING
--------------------------------------
 1. add the eight columns and the two FKs (they target `device_credential`,
    never `network_access`, so they are order-independent w.r.t. step 5);
 2. assert/log what is about to die (no `mode='tunnel'` row may exist; every
    non-default `network_access` row is dropped WITH the table, so its count is
    printed rather than silently discarded);
 3. FOLD each company's default outbound + default acs row into one
    `provisioning_settings` row, INSERTing when the tenant has none —
    `provisioning_settings` is lazily created, so absence is the NORMAL case and
    the INSERT branch is the only branch that runs on Railway development;
 4. backfill the two cwmp FKs from `device_credential.network_access_id` —
    BEFORE step 5, because that column is the ONLY thing identifying which
    credentials were the tenant's ACS Inform pair;
 5. drop `device_credential.network_access_id`, then `network_access`;
 6. add the CHECK constraints LAST, so a pre-existing inconsistency surfaces as
    a named constraint violation on real data rather than as an aborted DDL
    step mid-fold.

`enabled` IS WRITTEN `false` ON INSERT, DELIBERATELY. `ProvisioningSettings.enabled`
is a live provisioning gate (`backend-erp/utils/provisioning_guards.py`) and canon
C6 says absence of the row means DISABLED. A tenant that had `network_access`
rows but no `provisioning_settings` row therefore has provisioning OFF today, and
inserting `enabled=true` here would silently turn it ON with no operator action.
`default_inform_interval` is left NULL for the same reason.

WHAT IS NOT HERE, AND WHY
-------------------------
The spec asked for a sixth CHECK, `cwmp_pending_credential_id IS NULL OR
cwmp_credential_id IS NOT NULL` ("no pending without a current"). It is NOT
created, because it is violable by a DATABASE REFERENTIAL ACTION rather than only
by application code: both cwmp FKs are ON DELETE SET NULL, so deleting the
CURRENT credential while a rotation window is open sets `cwmp_credential_id` to
NULL with `cwmp_pending_credential_id` still set, the CHECK fails, and an ordinary
`DELETE /device-credentials/{id}` becomes an IntegrityError surfacing as a raw
500. A company delete has the same shape — `device_credential` and
`provisioning_settings` both CASCADE from `company` and Postgres does not order
the SET NULL against the CASCADE. The invariant belongs in the router as a 409 on
deleting either pointer (which also closes the pre-existing gap that a plain
DELETE of an ACS Inform credential was never rollout-gated). `ck_provisioning_settings_cwmp_pair`
IS created: SET NULL can never violate it.

downgrade() IS REVERSIBLE IN SUBSTANCE, NOT A TRUE INVERSE
----------------------------------------------------------
The mode reversal is exact and satisfies every recreated CHECK. Three genuine
losses, stated rather than papered over:

  * `network_access.name` is NOT NULL and UNIQUE per company
    (`uq_network_access_company_name`) with no destination column here, so
    downgrade SYNTHESIZES the deterministic names 'ACS' and 'Outbound' and will
    collide if a tenant separately holds a row of that name.
  * `mgmt_subnets` is gone for good — recreated as NULL.
  * A tenant that had a `provisioning_settings` row but never a `network_access`
    row is indistinguishable, after the fold, from a folded one. Downgrade
    creates rows for it too.
  * Only the TWO cwmp credentials are re-bound to the recreated `acs` row.
    Any OTHER `device_credential.network_access_id` binding (a company-default
    SSH/TELNET/WIREGUARD credential pinned to the outbound row) is not
    recoverable — the forward direction replaces that tier with
    "inventory_item_id IS NULL AND device_type_id IS NULL", which carries no
    row id to restore. Such a credential comes back unbound, i.e. still the
    company default under the new resolution order.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text

revision = "tr1_transport_axis"
down_revision = "ac1_acs_tenant_auth"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the
# nc1a/nat1/nat2/nat3/vpn1/na1/ac1 precedent (revisions are immutable, models are
# not, so neither may import the other). tests/test_transport_axis.py pins them
# equal.
_PROVISIONING_DIAL_TARGET_CHECK = "dial_target IN ('device','gateway')"
_PROVISIONING_PROXY_KIND_CHECK = "proxy_kind IN ('none','socks5')"
_PROVISIONING_PROXY_ADDRESS_CHECK = (
    "proxy_kind <> 'socks5' OR proxy_address IS NOT NULL"
)
_PROVISIONING_GATEWAY_HOST_CHECK = (
    "dial_target <> 'gateway' OR gateway_host IS NOT NULL"
)
# Written pending-first rather than the spec's `cwmp_credential_id IS NULL OR ...`
# so the intent is readable instead of resting on NULL-passes semantics. Identical
# truth table on both Postgres and the SQLite the test suite builds.
_PROVISIONING_CWMP_PAIR_CHECK = (
    "cwmp_pending_credential_id IS NULL "
    "OR cwmp_credential_id <> cwmp_pending_credential_id"
)

# nc1a's immutable copies, restored by downgrade().
_OLD_NETWORK_ACCESS_KIND_CHECK = "kind IN ('acs','outbound')"
_OLD_NETWORK_ACCESS_MODE_CHECK = (
    "mode IN ('direct','vpn','tunnel','nat_zt','nat_public')"
)
_OLD_NETWORK_ACCESS_NAT_GATEWAY_CHECK = (
    "mode NOT IN ('nat_zt','nat_public') OR gateway_host IS NOT NULL"
)
_OLD_NETWORK_ACCESS_PYLON_CHECK = "mode != 'nat_zt' OR pylon_socks5 IS NOT NULL"
_OLD_NETWORK_ACCESS_VPN_CHECK = "mode != 'vpn' OR vpn_socks5 IS NOT NULL"
_OLD_NETWORK_ACCESS_ACS_AUTH_CHECK = "kind = 'acs' OR acs_auth_required = false"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))

    # --- 1. the eight columns + the two credential FKs -----------------------
    op.add_column(
        "provisioning_settings",
        sa.Column(
            "dial_target", sa.String(), nullable=False, server_default="device"
        ),
    )
    op.add_column(
        "provisioning_settings",
        sa.Column("proxy_kind", sa.String(), nullable=False, server_default="none"),
    )
    op.add_column(
        "provisioning_settings", sa.Column("proxy_address", sa.String(), nullable=True)
    )
    op.add_column(
        "provisioning_settings", sa.Column("gateway_host", sa.String(), nullable=True)
    )
    op.add_column(
        "provisioning_settings", sa.Column("acs_base_url", sa.String(), nullable=True)
    )
    op.add_column(
        "provisioning_settings",
        sa.Column(
            "acs_auth_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "provisioning_settings",
        sa.Column("cwmp_credential_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "provisioning_settings",
        sa.Column("cwmp_pending_credential_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_provisioning_settings_cwmp_credential",
        "provisioning_settings",
        "device_credential",
        ["cwmp_credential_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_provisioning_settings_cwmp_pending_credential",
        "provisioning_settings",
        "device_credential",
        ["cwmp_pending_credential_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- 2. assert and LOG what is about to be destroyed ---------------------
    # 'tunnel' was reserved for canon C10's edge agent and the API rejected it
    # (backend-erp's _UNSHIPPED_MODES), so no stored row may carry it. It has no
    # destination on the new axis: fail loudly rather than guess.
    tunnels = connection.execute(
        sa.text("SELECT count(*) FROM network_access WHERE mode = 'tunnel'")
    ).scalar()
    if tunnels:
        raise RuntimeError(
            f"[tr1_transport_axis] {tunnels} network_access rows are in mode='tunnel', "
            "which the API never allowed and which has no mapping on the "
            "dial_target/proxy_kind axis. Resolve them (pick a real mode) first."
        )
    # Non-default rows are dropped WITH the table — they were unreachable
    # (resolve_endpoint only ever read `is_default`), but they are data and their
    # loss is recorded, not silent.
    strays = connection.execute(
        sa.text(
            "SELECT kind, mode, count(*) FROM network_access "
            "WHERE is_default IS NOT TRUE GROUP BY 1, 2 ORDER BY 1, 2"
        )
    ).fetchall()
    stray_total = sum(row[2] for row in strays)
    print(
        f"[tr1_transport_axis] dropping {stray_total} non-default network_access "
        f"row(s) with the table: {[tuple(r) for r in strays]}"
    )

    # --- 3. the fold: one provisioning_settings row per tenant --------------
    folded = connection.execute(
        sa.text(
            """
            WITH outbound AS (
                SELECT company_id, mode, gateway_host, pylon_socks5, vpn_socks5
                  FROM network_access
                 WHERE kind = 'outbound' AND is_default
            ),
            acs AS (
                SELECT company_id, acs_base_url, acs_auth_required
                  FROM network_access
                 WHERE kind = 'acs' AND is_default
            ),
            folded AS (
                SELECT c.company_id,
                       CASE WHEN o.mode IN ('nat_zt','nat_public')
                            THEN 'gateway' ELSE 'device' END          AS dial_target,
                       CASE WHEN o.mode IN ('nat_zt','vpn')
                            THEN 'socks5' ELSE 'none' END             AS proxy_kind,
                       CASE WHEN o.mode = 'nat_zt' THEN o.pylon_socks5
                            WHEN o.mode = 'vpn'    THEN o.vpn_socks5
                       END                                            AS proxy_address,
                       o.gateway_host,
                       a.acs_base_url,
                       COALESCE(a.acs_auth_required, false)           AS acs_auth_required
                  FROM (SELECT DISTINCT company_id FROM network_access) c
                  LEFT JOIN outbound o ON o.company_id = c.company_id
                  LEFT JOIN acs a      ON a.company_id = c.company_id
            )
            INSERT INTO provisioning_settings (
                id, created_at, updated_at, company_id, enabled,
                default_inform_interval, dial_target, proxy_kind, proxy_address,
                gateway_host, acs_base_url, acs_auth_required
            )
            SELECT gen_random_uuid(), NOW(), NOW(), company_id, false, NULL,
                   dial_target, proxy_kind, proxy_address, gateway_host,
                   acs_base_url, acs_auth_required
              FROM folded
            ON CONFLICT (company_id) DO UPDATE SET
                dial_target       = EXCLUDED.dial_target,
                proxy_kind        = EXCLUDED.proxy_kind,
                proxy_address     = EXCLUDED.proxy_address,
                gateway_host      = EXCLUDED.gateway_host,
                acs_base_url      = EXCLUDED.acs_base_url,
                acs_auth_required = EXCLUDED.acs_auth_required,
                updated_at        = NOW()
            """
        )
    ).rowcount
    print(f"[tr1_transport_axis] folded {folded} tenant transport row(s)")

    # --- 4. the cwmp pair, BEFORE network_access_id disappears --------------
    # `device_credential.network_access_id` is the ONLY thing that says which
    # credentials were this tenant's ACS Inform pair; after step 5 the
    # information is unrecoverable. Newest HTTP_BASIC bound to the default acs
    # row is the current secret, second-newest the pending one — the exact
    # `created_at DESC, id DESC` order the accept-both window relied on.
    paired = connection.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT dc.id, dc.company_id,
                       row_number() OVER (
                           PARTITION BY dc.company_id
                           ORDER BY dc.created_at DESC, dc.id DESC
                       ) AS rn
                  FROM device_credential dc
                  JOIN network_access na ON na.id = dc.network_access_id
                 WHERE dc.kind = 'HTTP_BASIC'
                   AND na.kind = 'acs'
                   AND na.is_default
            )
            UPDATE provisioning_settings ps
               SET cwmp_credential_id =
                     (SELECT id FROM ranked WHERE company_id = ps.company_id AND rn = 1),
                   cwmp_pending_credential_id =
                     (SELECT id FROM ranked WHERE company_id = ps.company_id AND rn = 2),
                   updated_at = NOW()
             WHERE EXISTS (SELECT 1 FROM ranked WHERE company_id = ps.company_id)
            """
        )
    ).rowcount
    print(f"[tr1_transport_axis] carried the cwmp credential pair for {paired} tenant(s)")

    # --- 5. drop the binding column, then the table -------------------------
    # Dropping the column takes device_credential_network_access_id_fkey and
    # ix_device_credential_network_access_id with it; that FK is the only thing
    # pointing at network_access, so the table drop is then unblocked.
    op.drop_column("device_credential", "network_access_id")
    op.drop_table("network_access")

    # --- 6. the CHECKs, after the data is in place ---------------------------
    op.create_check_constraint(
        "ck_provisioning_settings_dial_target",
        "provisioning_settings",
        sa.text(_PROVISIONING_DIAL_TARGET_CHECK),
    )
    op.create_check_constraint(
        "ck_provisioning_settings_proxy_kind",
        "provisioning_settings",
        sa.text(_PROVISIONING_PROXY_KIND_CHECK),
    )
    op.create_check_constraint(
        "ck_provisioning_settings_proxy_address",
        "provisioning_settings",
        sa.text(_PROVISIONING_PROXY_ADDRESS_CHECK),
    )
    op.create_check_constraint(
        "ck_provisioning_settings_gateway_host",
        "provisioning_settings",
        sa.text(_PROVISIONING_GATEWAY_HOST_CHECK),
    )
    op.create_check_constraint(
        "ck_provisioning_settings_cwmp_pair",
        "provisioning_settings",
        sa.text(_PROVISIONING_CWMP_PAIR_CHECK),
    )

    print("[tr1_transport_axis] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))

    op.drop_constraint(
        "ck_provisioning_settings_cwmp_pair", "provisioning_settings", type_="check"
    )
    op.drop_constraint(
        "ck_provisioning_settings_gateway_host", "provisioning_settings", type_="check"
    )
    op.drop_constraint(
        "ck_provisioning_settings_proxy_address", "provisioning_settings", type_="check"
    )
    op.drop_constraint(
        "ck_provisioning_settings_proxy_kind", "provisioning_settings", type_="check"
    )
    op.drop_constraint(
        "ck_provisioning_settings_dial_target", "provisioning_settings", type_="check"
    )

    # --- recreate the table exactly as nc1a + nat1/nat2/nat3/vpn1/na1/ac1 left it
    op.create_table(
        "network_access",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="direct"),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("mgmt_subnets", sa.JSON(), nullable=True),
        sa.Column("acs_base_url", sa.String(), nullable=True),
        sa.Column("gateway_host", sa.String(), nullable=True),
        sa.Column("pylon_socks5", sa.String(), nullable=True),
        sa.Column("vpn_socks5", sa.String(), nullable=True),
        sa.Column(
            "acs_auth_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", name="uq_network_access_company_name"),
        sa.CheckConstraint(_OLD_NETWORK_ACCESS_KIND_CHECK, name="ck_network_access_kind"),
        sa.CheckConstraint(_OLD_NETWORK_ACCESS_MODE_CHECK, name="ck_network_access_mode"),
        sa.CheckConstraint(
            _OLD_NETWORK_ACCESS_NAT_GATEWAY_CHECK,
            name="ck_network_access_nat_gateway_host",
        ),
        sa.CheckConstraint(
            _OLD_NETWORK_ACCESS_PYLON_CHECK, name="ck_network_access_pylon_socks5"
        ),
        sa.CheckConstraint(
            _OLD_NETWORK_ACCESS_VPN_CHECK, name="ck_network_access_vpn_socks5"
        ),
        sa.CheckConstraint(
            _OLD_NETWORK_ACCESS_ACS_AUTH_CHECK,
            name="ck_network_access_acs_auth_required",
        ),
    )
    op.create_index("ix_network_access_company_id", "network_access", ["company_id"])
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_network_access_default "
        "ON network_access (company_id, kind) WHERE is_default"
    )

    # --- move the values back into one outbound + one acs row per tenant ----
    # Both rows are is_default (uq_network_access_default is per (company_id,
    # kind), so one of each is exactly what it permits), and the recreated
    # CHECKs are all satisfied by construction: the forward
    # gateway_host/proxy_address CHECKs guarantee the operands are non-NULL
    # wherever the reversed mode requires them.
    restored = connection.execute(
        sa.text(
            """
            INSERT INTO network_access (
                id, created_at, updated_at, name, kind, mode, is_default,
                mgmt_subnets, acs_base_url, gateway_host, pylon_socks5,
                vpn_socks5, acs_auth_required, company_id
            )
            SELECT gen_random_uuid(), NOW(), NOW(), 'Outbound', 'outbound',
                   CASE WHEN ps.dial_target = 'gateway' AND ps.proxy_kind = 'socks5'
                             THEN 'nat_zt'
                        WHEN ps.dial_target = 'gateway' THEN 'nat_public'
                        WHEN ps.proxy_kind  = 'socks5'  THEN 'vpn'
                        ELSE 'direct' END,
                   true, NULL, NULL, ps.gateway_host,
                   CASE WHEN ps.dial_target = 'gateway' AND ps.proxy_kind = 'socks5'
                             THEN ps.proxy_address END,
                   CASE WHEN ps.dial_target = 'device' AND ps.proxy_kind = 'socks5'
                             THEN ps.proxy_address END,
                   false, ps.company_id
              FROM provisioning_settings ps
            """
        )
    ).rowcount
    connection.execute(
        sa.text(
            """
            INSERT INTO network_access (
                id, created_at, updated_at, name, kind, mode, is_default,
                mgmt_subnets, acs_base_url, gateway_host, pylon_socks5,
                vpn_socks5, acs_auth_required, company_id
            )
            SELECT gen_random_uuid(), NOW(), NOW(), 'ACS', 'acs', 'direct', true,
                   NULL, ps.acs_base_url, NULL, NULL, NULL,
                   ps.acs_auth_required, ps.company_id
              FROM provisioning_settings ps
            """
        )
    )
    print(f"[tr1_transport_axis] recreated network_access rows for {restored} tenant(s)")

    # --- give device_credential its binding column back, and re-bind the pair
    op.add_column(
        "device_credential", sa.Column("network_access_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "device_credential_network_access_id_fkey",
        "device_credential",
        "network_access",
        ["network_access_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_device_credential_network_access_id",
        "device_credential",
        ["network_access_id"],
    )
    connection.execute(
        sa.text(
            """
            UPDATE device_credential dc
               SET network_access_id = na.id
              FROM provisioning_settings ps
              JOIN network_access na
                ON na.company_id = ps.company_id
               AND na.kind = 'acs'
               AND na.is_default
             WHERE dc.id IN (ps.cwmp_credential_id, ps.cwmp_pending_credential_id)
            """
        )
    )

    op.drop_constraint(
        "fk_provisioning_settings_cwmp_pending_credential",
        "provisioning_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_provisioning_settings_cwmp_credential",
        "provisioning_settings",
        type_="foreignkey",
    )
    for column in (
        "cwmp_pending_credential_id",
        "cwmp_credential_id",
        "acs_auth_required",
        "acs_base_url",
        "gateway_host",
        "proxy_address",
        "proxy_kind",
        "dial_target",
    ):
        op.drop_column("provisioning_settings", column)

    print("[tr1_transport_axis] downgrade complete")
