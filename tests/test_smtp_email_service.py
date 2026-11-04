from unittest.mock import AsyncMock, patch

import pytest

from database_utils.services.email_service import SMTPEmailService


@pytest.fixture
def smtp_service():
    return SMTPEmailService(
        host="smtp.hostinger.com",
        port=465,
        username="noreply@example.com",
        password="secret",
        from_address="noreply@example.com",
        use_tls=True,
    )


@pytest.mark.asyncio
async def test_send_confirmation_email_calls_smtp_send(smtp_service):
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await smtp_service.send_confirmation_email(
            to_email="user@example.com",
            confirmation_link="https://app.example.com/confirm-email?token=abc",
            user_name="Jane",
        )
    assert result is True
    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["hostname"] == "smtp.hostinger.com"
    assert kwargs["port"] == 465


@pytest.mark.asyncio
async def test_send_confirmation_email_returns_false_on_error(smtp_service):
    with patch("aiosmtplib.send", new_callable=AsyncMock, side_effect=OSError("connection refused")):
        result = await smtp_service.send_confirmation_email(
            to_email="user@example.com",
            confirmation_link="https://app.example.com/confirm-email?token=abc",
            user_name="Jane",
        )
    assert result is False


@pytest.mark.asyncio
async def test_send_generic_sets_display_name_and_reply_to(smtp_service):
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        ok = await smtp_service.send_generic(
            "client@example.com", "Hola\nmundo", "<p>hi</p>",
            from_name="Cable Sur, S.A.", reply_to="office@cablesur.example",
        )
    assert ok is True
    msg = mock_send.call_args.args[0]
    assert msg["From"] == '"Cable Sur, S.A." <noreply@example.com>'
    assert msg["Reply-To"] == "office@cablesur.example"
    assert msg["Subject"] == "Hola mundo" and msg["To"] == "client@example.com"


@pytest.mark.asyncio
async def test_send_generic_without_name_or_reply_to(smtp_service):
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        assert await smtp_service.send_generic("c@example.com", "s", "<p>x</p>") is True
    msg = mock_send.call_args.args[0]
    assert msg["From"] == "noreply@example.com" and msg["Reply-To"] is None


@pytest.mark.asyncio
async def test_mock_send_generic():
    from database_utils.services.email_service import MockEmailService
    assert await MockEmailService().send_generic("c@example.com", "s", "<p>x</p>", "Co", "r@x.com") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"from_name": "ISP\nX"},
    {"reply_to": "r@x.com\nBcc: evil@x.com"},
    {"to_email": "c@example.com\r\nBcc: evil@x.com"},
])
async def test_send_generic_returns_false_on_linebreak_header(smtp_service, kwargs):
    args = {"to_email": "c@example.com", "subject": "s", "html_body": "<p>x</p>", **kwargs}
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        assert await smtp_service.send_generic(**args) is False
    mock_send.assert_not_called()
