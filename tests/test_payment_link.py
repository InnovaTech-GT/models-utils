"""pl1_payment_link en SQLite: los ajustes de la integración RECURRENTE, el
proveedor nuevo y los índices únicos parciales del modelo (un link activo por
orden, un intento por link). Los CHECK viven solo en la revisión: ver
tests/pg/test_pl1_pg.py."""
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import CO_A
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    ACTIVE_PAYMENT_LINK_STATUSES,
    PaymentLink,
    PaymentLinkStatus,
)
from database_utils.schemas.integration import IntegrationCreate, IntegrationOut
from database_utils.schemas.payment_link import RecurrenteSettings
from database_utils.utils.timezone_utils import now_gt


def _link(order_id, status=PaymentLinkStatus.PENDING, **kw):
    datos = {
        "company_id": CO_A, "client_id": uuid.uuid4(), "order_id": order_id, "status": status,
        "amount_cents": 15000, "payment_methods": ["bank_transfer"],
        "expires_at": now_gt() + timedelta(hours=120),
        "checkout_id": f"ch_{uuid.uuid4().hex[:16]}", "checkout_url": "https://app.recurrente.com/checkout-session/x",
    }
    datos.update(kw)
    return PaymentLink(**datos)


# --- ajustes de la integración ------------------------------------------------

def test_ajustes_por_defecto_solo_transferencia_y_cinco_dias():
    s = RecurrenteSettings()
    assert s.payment_methods == ["bank_transfer"]
    assert s.link_hours == 120


def test_ajustes_aceptan_tarjeta_y_transferencia():
    assert RecurrenteSettings(payment_methods=["card", "bank_transfer"]).payment_methods == ["card", "bank_transfer"]


@pytest.mark.parametrize("datos", [
    {"payment_methods": []},                          # ninguno
    {"payment_methods": ["bank_transfer", "bank_transfer"]},  # repetido
    {"payment_methods": ["cash"]},                    # no lo ofrece Recurrente por link
    {"payment_methods": ["stablecoins"]},             # no permitido en Uplink
    {"link_hours": 0},
    {"link_hours": 721},
    {"api_key": "sk_test_x"},                         # la llave no va en los ajustes
])
def test_ajustes_invalidos(datos):
    with pytest.raises(ValidationError):
        RecurrenteSettings(**datos)


# --- proveedor de la integración --------------------------------------------

def test_proveedor_recurrente_aceptado_y_desconocido_rechazado():
    base = {"name": "Recurrente", "base_url": "https://app.recurrente.com/api"}
    assert IntegrationCreate(**base, provider="RECURRENTE").provider == "RECURRENTE"
    with pytest.raises(ValidationError):
        IntegrationCreate(**base, provider="PAYPAL")


def test_la_llave_de_recurrente_sale_enmascarada():
    fila = SimpleNamespace(  # lo mínimo que lee IntegrationOut
        id=uuid.uuid4(), company_id=CO_A, name="Recurrente", description=None,
        base_url="https://app.recurrente.com/api", auth_type="API_KEY",
        credentials={"header_name": "X-SECRET-KEY", "api_key": "sk_test_secreta",
                     "payment_methods": ["bank_transfer"], "link_hours": 120},
        provider="RECURRENTE", enabled=True, created_at=now_gt(), updated_at=None,
    )
    out = IntegrationOut.from_orm_masked(fila)
    assert out.credentials["api_key"] == "***"
    assert "sk_test_secreta" not in out.model_dump_json()
    assert out.credentials["header_name"] == "X-SECRET-KEY"
    assert out.credentials["payment_methods"] == ["bank_transfer"]


# --- modelo -------------------------------------------------------------------

def test_estados_activos():
    assert set(ACTIVE_PAYMENT_LINK_STATUSES) == {PaymentLinkStatus.CREATING, PaymentLinkStatus.PENDING}


def test_valores_por_defecto(db):
    link = PaymentLink(company_id=CO_A, client_id=uuid.uuid4(), order_id=uuid.uuid4(),
                       amount_cents=15000, expires_at=now_gt() + timedelta(hours=1))
    db.add(link); db.commit(); db.refresh(link)
    assert link.status == PaymentLinkStatus.CREATING
    assert link.provider == "RECURRENTE"
    assert link.currency == "GTQ"
    assert link.checks == 0


def test_dos_links_activos_para_la_misma_orden_no(db):
    orden = uuid.uuid4()
    db.add(_link(orden)); db.commit()
    db.add(_link(orden, status=PaymentLinkStatus.CREATING, checkout_id=None, checkout_url=None))
    with pytest.raises(IntegrityError):
        db.commit()


def test_un_link_vencido_no_bloquea_uno_nuevo(db):
    orden = uuid.uuid4()
    db.add(_link(orden, status=PaymentLinkStatus.EXPIRED))
    db.add(_link(orden, status=PaymentLinkStatus.REVIEW, review_reason="AMOUNT_CHANGED"))
    db.add(_link(orden, status=PaymentLinkStatus.FAILED, checkout_id=None, checkout_url=None))
    db.add(_link(orden))
    db.commit()
    assert db.query(PaymentLink).filter_by(order_id=orden).count() == 4


def test_el_mismo_checkout_no_se_guarda_dos_veces(db):
    db.add(_link(uuid.uuid4(), checkout_id="ch_repetido")); db.commit()
    db.add(_link(uuid.uuid4(), checkout_id="ch_repetido"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_el_mismo_intento_no_paga_dos_links(db):
    db.add(_link(uuid.uuid4(), status=PaymentLinkStatus.EXPIRED, intent_id="in_unico")); db.commit()
    db.add(_link(uuid.uuid4(), status=PaymentLinkStatus.EXPIRED, intent_id="in_unico"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_varios_links_sin_checkout_ni_intento_si(db):
    for _ in range(3):
        db.add(_link(uuid.uuid4(), status=PaymentLinkStatus.FAILED, checkout_id=None, checkout_url=None))
    db.commit()
