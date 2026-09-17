import pytest
from jinja2 import TemplateNotFound

from database_utils.utils.email_templates import render_email


def test_render_confirmation_template_includes_link_and_name():
    html = render_email(
        "confirmation.html",
        user_name="Jane",
        confirmation_link="https://app.example.com/confirm-email?token=abc",
    )
    assert "Jane" in html
    assert "https://app.example.com/confirm-email?token=abc" in html


def test_render_confirmation_template_locales():
    ctx = {
        "user_name": "Jane",
        "confirmation_link": "https://app.example.com/es/confirm-email?token=abc",
    }
    es = render_email("confirmation.html", locale="es", **ctx)
    en = render_email("confirmation.html", locale="en", **ctx)
    assert "Confirma tu correo" in es
    assert "Confirm your email" in en
    for html in (es, en):
        assert "Uplink" in html
        assert ctx["confirmation_link"] in html


def test_render_email_unknown_locale_falls_back_to_spanish():
    html = render_email(
        "welcome.html", locale="fr", user_name="Jane"
    )
    assert "Bienvenido a Uplink" in html


def test_render_invitation_template_locales():
    ctx = {
        "invitation_link": "https://app.example.com/es/accept-invitation?token=abc",
        "company_name": "Acme Inc",
        "invited_by": "Admin",
    }
    es = render_email("invitation.html", locale="es", **ctx)
    en = render_email("invitation.html", locale="en", **ctx)
    assert "invitado" in es
    assert "invited" in en
    for html in (es, en):
        assert "Acme Inc" in html
        assert ctx["invitation_link"] in html


def test_render_password_reset_template():
    html = render_email(
        "password_reset.html",
        locale="en",
        user_name="Jane",
        reset_link="https://app.example.com/reset-password?token=abc",
    )
    assert "https://app.example.com/reset-password?token=abc" in html
    assert "Reset your password" in html


def test_render_payment_receipt_template():
    html = render_email(
        "payment_receipt.html",
        company_name="Acme Inc",
        invoice_number="INV-1",
        total_formatted="$99.00",
    )
    assert "Acme Inc" in html
    assert "INV-1" in html
    assert "$99.00" in html


def test_render_join_request_decision_approved():
    html = render_email(
        "join_request_decision.html",
        user_name="Jane",
        company_name="Acme Inc",
        approved=True,
    )
    assert "Acme Inc" in html


def test_render_missing_template_raises():
    with pytest.raises(TemplateNotFound):
        render_email("does_not_exist.html", locale="es")
