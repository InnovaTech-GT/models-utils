"""wb1_whatsapp_bot_config: menú del bot de WhatsApp (Nova) por cablera.

Una fila por company. `options` es una lista JSON que se valida con
database_utils.schemas.whatsapp_bot en cada escritura; el gateway la lee
a través del endpoint whatsapp-config de backend-erp.
"""
import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from database_utils.database import Base
from ..utils.timezone_utils import now_gt


class WhatsAppBotConfig(Base):
    __tablename__ = "whatsapp_bot_config"
    __table_args__ = (
        Index("uq_whatsapp_bot_config_company", "company_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    # false = el bot ignora esta config y usa el menú predeterminado.
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    welcome_text = Column(String, nullable=False)
    menu_button_text = Column(String, nullable=False, default="Ver opciones")
    options = Column(JSON, nullable=False, default=list)
