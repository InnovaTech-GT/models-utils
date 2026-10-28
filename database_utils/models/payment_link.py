"""pl1_payment_link: links de pago en línea por orden (hoy solo Recurrente).

Una fila por link que Uplink le pidió a la pasarela. La verdad sobre si se
pagó NUNCA sale de esta fila ni de lo que diga el cliente: backend-erp la
confirma consultando a la pasarela con la llave de la cablera y registra el
pago con PaymentService. Esta tabla solo recuerda qué links son nuestros,
por cuánto y en qué quedaron.
"""
import enum
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from database_utils.database import Base

from ..utils.timezone_utils import now_gt

PAYMENT_LINK_PROVIDERS = ("RECURRENTE",)
# Métodos que se le ofrecen al pagador (valores de Recurrente).
PAYMENT_LINK_METHODS = ("bank_transfer", "card")
PAYMENT_LINK_MIN_CENTS = 500  # Recurrente: mínimo Q5


class PaymentLinkStatus(str, enum.Enum):
    CREATING = "CREATING"  # fila escrita; la pasarela aún no devolvió el checkout
    PENDING = "PENDING"    # link vigente, esperando el pago
    PAID = "PAID"          # confirmado con la pasarela y registrado en PaymentService
    EXPIRED = "EXPIRED"    # venció sin pago confirmado
    REVIEW = "REVIEW"      # la pasarela dice pagado pero algo no cuadra: revisión manual
    FAILED = "FAILED"      # no se pudo crear el checkout


# Un link "activo" bloquea crear otro para la misma orden (índice parcial).
ACTIVE_PAYMENT_LINK_STATUSES = (PaymentLinkStatus.CREATING, PaymentLinkStatus.PENDING)


class PaymentLink(Base):
    __tablename__ = "payment_link"
    # Los CHECK viven solo en la revisión pl1 (estilo de la casa).
    __table_args__ = (
        Index("ix_payment_link_company_status", "company_id", "status"),
        Index("ix_payment_link_order", "order_id"),
        Index("uq_payment_link_checkout", "checkout_id", unique=True,
              postgresql_where=text("checkout_id IS NOT NULL"),
              sqlite_where=text("checkout_id IS NOT NULL")),
        Index("uq_payment_link_intent", "intent_id", unique=True,
              postgresql_where=text("intent_id IS NOT NULL"),
              sqlite_where=text("intent_id IS NOT NULL")),
        Index("uq_payment_link_active_order", "order_id", unique=True,
              postgresql_where=text("status IN ('CREATING', 'PENDING')"),
              sqlite_where=text("status IN ('CREATING', 'PENDING')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)

    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # RESTRICT: una orden con links (y quizá dinero) no desaparece.
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("order.id", ondelete="RESTRICT"), nullable=False
    )
    # SET NULL: si borran la integración, el historial queda; un link
    # pendiente sin integración ya no se puede confirmar y pasa a REVIEW.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("integration.id", ondelete="SET NULL"), nullable=True
    )

    provider = Column(String(32), nullable=False, default="RECURRENTE")
    status = Column(
        Enum(PaymentLinkStatus, native_enum=False, length=16, create_constraint=False),
        nullable=False, default=PaymentLinkStatus.CREATING,
    )

    # Lo que se le pidió a la pasarela; se compara contra su respuesta al confirmar.
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="GTQ")
    payment_methods = Column(JSON, nullable=False, default=list)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # Lo que devolvió la pasarela. checkout_url se guarda porque el GET no la trae.
    checkout_id = Column(String(64), nullable=True)
    checkout_url = Column(String(512), nullable=True)
    live_mode = Column(Boolean, nullable=True)

    # Revisión periódica.
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    checks = Column(Integer, nullable=False, default=0, server_default="0")

    # Confirmación. intent_id es único: un intento de la pasarela registra
    # como mucho un pago (además del idempotency_key de PaymentService).
    intent_id = Column(String(64), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payment.id", ondelete="RESTRICT"), nullable=True
    )
    paid_at = Column(DateTime(timezone=True), nullable=True)
    review_reason = Column(String(64), nullable=True)
