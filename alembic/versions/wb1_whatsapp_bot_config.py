"""whatsapp_bot_config: menú del bot de WhatsApp (Nova) por cablera

Revision ID: wb1_whatsapp_bot_config
Revises: ca1_collection_areas
Create Date: 2026-10-21

whatsapp_bot_config  (una fila por company; options = JSON validado por
database_utils.schemas.whatsapp_bot). Sin fila -> el bot usa el menú
predeterminado. Aditiva: tabla nueva + índice único. Downgrade la borra.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "wb1_whatsapp_bot_config"
down_revision: Union[str, Sequence[str], None] = "ca1_collection_areas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("""
        CREATE TABLE IF NOT EXISTS whatsapp_bot_config (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT true,
            welcome_text VARCHAR NOT NULL,
            menu_button_text VARCHAR NOT NULL DEFAULT 'Ver opciones',
            options JSON NOT NULL DEFAULT '[]'::json
        )
    """))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_bot_config_company "
        "ON whatsapp_bot_config (company_id)"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("DROP TABLE IF EXISTS whatsapp_bot_config"))
