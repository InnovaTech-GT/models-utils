"""Capa 3: per-tenant CWMP Inform authentication gate + reveal permission

Revision ID: ac1_acs_tenant_auth
Revises: na1_kind_outbound
Create Date: 2026-09-25

A CPE identifies itself to GenieACS by serial number alone — data printed on
the device label — so tenant attribution today rests on public information.
Capa 3 adds credential proof: GenieACS's `cwmp.auth` expression calls back into
Uplink, which answers "does this tenant require auth?" and "what is the
expected plaintext?". Tenant attribution itself stays serial-derived
(no GenieACS patching); the password only authenticates it.

THREE things land here, and nothing else:

1. `network_access.acs_auth_required` — the per-tenant switch, BOOLEAN NOT NULL
   server_default false. Default-OFF is a DB constraint, not app code: a tenant
   that never enrols authenticates exactly as it does today, and OFF means
   ALLOW. (A serial with no acs_device_registration row also means ALLOW —
   that half is backend/ext code. Both are required or auto-discovery and
   quarantine break.)

2. `ck_network_access_acs_auth_required` = "kind = 'acs' OR acs_auth_required =
   false". The gate is only meaningful on the tenant's default kind='acs' row,
   which is the only row the inform-auth lookup joins. The CHECK is what stops
   a raw UPDATE arming it on an outbound row where nothing would ever read it.

3. `uq_acs_registration_serial_no_oui` — a partial UNIQUE on
   `acs_device_registration (serial_number) WHERE oui IS NULL`, and it is a
   multi-tenancy fix, not housekeeping. `uq_acs_registration_identity` is a
   plain two-column UNIQUE and Postgres treats NULLs as distinct, while `oui`
   IS nullable and `_normalize_oui` returns None unchanged for an omitted OUI
   — so `(NULL, 'SN1')` can appear any number of times today. The router's 409
   is check-then-insert with no DB backstop. Once the gate is armed, the
   inform-auth lookup's `.first()` over a duplicated serial would hand one
   tenant's CWMP password to another tenant's CPE. Created with a pre-check so
   an existing duplicate fails loudly with the offending serials named instead
   of as a bare index-build error.

Plus the `device_credentials.reveal` permission row and its NOC grant
(cfg3 recipe: idempotent INSERT ... ON CONFLICT DO NOTHING, per-role grant,
post-upgrade count assertion). ADMIN and MANAGER are NOT granted here —
isp_seed._seed_permissions cross-joins every ISP_PERMISSIONS name onto global
ADMIN and MANAGER minus ADMIN_ONLY_PERMISSIONS, and this name IS in that tuple
(and in rbac_seed.MANAGER_EXCLUDED_PERMISSIONS, pinned by
tests/test_attested_adoption.py), so it reaches ADMIN and NOC and not MANAGER.

WHAT IS DELIBERATELY NOT HERE — the rotation window's second secret.
`DeviceCredential` has exactly one secret slot and `POST /{id}/rotate`
overwrites it in place, so decision 9's accept-both window needs somewhere to
read a second secret from. Per decision 12 that place is a SECOND
`DeviceCredential` row bound to the same kind='acs' `network_access` row
(`informPassword` = newest, `informPendingPassword` = second-newest; rotation
is create-new -> roll out -> delete-old, and `POST /{id}/rotate` is simply not
used for this credential). So: no pending_* columns, and deliberately NO
partial unique index on (company_id, network_access_id) — such an index would
forbid the very second row the window is made of.

Dropped, never merged, and recorded here so the decision is findable: revision
`nc1d_acs_tenant_credentials` (branch feat/network-config/acs-tenant-credentials)
stored a bcrypt hash of the tenant password. It cannot work — the comparison
happens inside GenieACS via `AUTH(username, password)`, which needs the
expected PLAINTEXT (`cwmp.ts` does `authentication["password"] === e[3]` for
Basic and feeds the plaintext into the Digest computation); there is no
hand-GenieACS-a-hash hook and patching GenieACS is out of scope. The
standardization rule that replaces it is in docs/network-models.md: can the
system ever need the original value back? No -> bcrypt. Yes -> `encrypt_secret`
envelope AES-256-GCM.

downgrade() reverses everything except nothing — there is no data conversion in
this revision to be lossy about.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text

revision = "ac1_acs_tenant_auth"
down_revision = "na1_kind_outbound"
branch_labels = None
depends_on = None

# Duplicated byte-for-byte from database_utils/models/isp.py — the nc1a/nat1/
# nat2/nat3 precedent. tests/test_vpn_transport_constants.py pins them equal.
_NETWORK_ACCESS_ACS_AUTH_CHECK = "kind = 'acs' OR acs_auth_required = false"

# Pinned against alembic/seeds/isp_seed.py ISP_PERMISSIONS by
# tests/test_vpn_transport_constants.py (revisions are immutable, seeds are
# not — neither can import the other; cfg3/ba1 precedent).
PERMISSIONS = [
    {
        "name": "device_credentials.reveal",
        "resource": "device_credentials",
        "action": "reveal",
        "description": "Reveal a device credential's plaintext secret (audited)",
    },
]

# ADMIN/MANAGER are NOT listed: the convergent seed reconciler grants ADMIN and
# withholds this name from MANAGER via ADMIN_ONLY_PERMISSIONS /
# MANAGER_EXCLUDED_PERMISSIONS. Duplicating that here would be a second source
# of truth. Pinned against isp_seed.ISP_ROLES by the same test.
GRANTS = (("NOC", "device_credentials.reveal"),)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))

    op.add_column(
        "network_access",
        sa.Column(
            "acs_auth_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "ck_network_access_acs_auth_required",
        "network_access",
        sa.text(_NETWORK_ACCESS_ACS_AUTH_CHECK),
    )

    # Pre-check before the index build, so a pre-existing duplicate names
    # itself instead of aborting the whole release with a bare PG error (an
    # aborted migrate job blocks every backend: they depend_on it).
    dupes = connection.execute(
        sa.text(
            "SELECT serial_number, count(*) AS n FROM acs_device_registration "
            "WHERE oui IS NULL GROUP BY 1 HAVING count(*) > 1"
        )
    ).fetchall()
    if dupes:
        raise RuntimeError(
            "[ac1_acs_tenant_auth] cannot create uq_acs_registration_serial_no_oui — "
            f"these serials already have more than one NULL-oui registration: {dupes}. "
            "Two tenants claiming one serial is the exact cross-tenant leak this index "
            "exists to prevent; resolve the duplicates (delete or set a real oui) first."
        )
    op.create_index(
        "uq_acs_registration_serial_no_oui",
        "acs_device_registration",
        ["serial_number"],
        unique=True,
        postgresql_where=text("oui IS NULL"),
    )

    for perm in PERMISSIONS:
        connection.execute(
            text(
                "INSERT INTO permission (id, created_at, name, resource, action, description) "
                "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            perm,
        )
    for role_name, perm_name in GRANTS:
        connection.execute(
            text(
                "INSERT INTO role_permission (role_id, permission_id) "
                "SELECT r.id, p.id FROM role r, permission p "
                "WHERE r.name = :role AND r.company_id IS NULL AND p.name = :perm "
                "ON CONFLICT DO NOTHING"
            ),
            {"role": role_name, "perm": perm_name},
        )

    names = ", ".join(f"'{p['name']}'" for p in PERMISSIONS)
    found = connection.execute(
        text(f"SELECT COUNT(*) FROM permission WHERE name IN ({names})")
    ).scalar()
    if found != len(PERMISSIONS):
        raise RuntimeError(
            f"[ac1_acs_tenant_auth] expected {len(PERMISSIONS)} permission rows "
            f"after upgrade, found {found}"
        )

    print("[ac1_acs_tenant_auth] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET lock_timeout = '5s'"))

    names = ", ".join(f"'{p['name']}'" for p in PERMISSIONS)
    # Drops every grant, including the ADMIN one the seed converged and any a
    # tenant added to a custom role — the permission row is going away, so
    # nothing may be left pointing at it. Same caveat as cfg3/ba1: env.py runs
    # the seeds after EVERY online alembic command, downgrades included, so a
    # downgrade through env.py re-inserts these rows unless the seed edits are
    # reverted too.
    connection.execute(
        text(
            "DELETE FROM role_permission WHERE permission_id IN "
            f"(SELECT id FROM permission WHERE name IN ({names}))"
        )
    )
    connection.execute(text(f"DELETE FROM permission WHERE name IN ({names})"))

    op.drop_index(
        "uq_acs_registration_serial_no_oui", table_name="acs_device_registration"
    )
    op.drop_constraint(
        "ck_network_access_acs_auth_required", "network_access", type_="check"
    )
    op.drop_column("network_access", "acs_auth_required")

    print("[ac1_acs_tenant_auth] downgrade complete")
