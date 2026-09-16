"""cycle 5 phase 1 (identity-theft mitigation, "Capa 3"): per-device CWMP Inform-time credentials on acs_device_registration

Revision ID: nc1c_cwmp_inform_credentials
Revises: 6e7506e57be9
Create Date: 2026-09-14

Adds the columns backing the Inform-direction CWMP credential (the CPE
proves it knows a per-device secret when it informs GenieACS, not just its
serial/OUI which are public data printed on a physical label). Mirrors the
existing cwmp_cr_* (Connection-Request direction) columns added in
nc1a_network_config_core, same envelope-encryption scheme (crypto.py).

No username column here (unlike cwmp_cr_username) — the username side of
GenieACS's AUTH() check is the device's own reported DeviceID.SerialNumber,
there is nothing tenant-specific to store for it.

All columns nullable, backfill-free: existing rows simply have no Inform
credential yet, and get one lazily issued on first getPassword lookup
(same pattern as cwmp_cr_* at bootstrap).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'nc1c_cwmp_inform_credentials'
down_revision: Union[str, Sequence[str], None] = '6e7506e57be9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('acs_device_registration', sa.Column('cwmp_inform_secret_ciphertext', sa.LargeBinary(), nullable=True))
    op.add_column('acs_device_registration', sa.Column('cwmp_inform_dek_wrapped', sa.LargeBinary(), nullable=True))
    op.add_column('acs_device_registration', sa.Column('cwmp_inform_kek_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('acs_device_registration', 'cwmp_inform_kek_id')
    op.drop_column('acs_device_registration', 'cwmp_inform_dek_wrapped')
    op.drop_column('acs_device_registration', 'cwmp_inform_secret_ciphertext')
