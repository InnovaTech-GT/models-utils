from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any, Dict
from loguru import logger
import aiosmtplib

from database_utils.utils.email_templates import render_email


# Localized subjects for the Uplink transactional emails that carry a locale.
# Unknown locales fall back to Spanish (the platform default).
_UPLINK_SUBJECTS: Dict[str, Dict[str, str]] = {
    "confirmation": {
        "es": "Confirma tu correo — Uplink",
        "en": "Confirm your email — Uplink",
    },
    "invitation": {
        "es": "Invitación a {company_name} — Uplink",
        "en": "Invitation to {company_name} — Uplink",
    },
    "password_reset": {
        "es": "Restablecer contraseña — Uplink",
        "en": "Reset your password — Uplink",
    },
    "welcome": {
        "es": "Bienvenido a Uplink",
        "en": "Welcome to Uplink",
    },
}


def _localized_subject(kind: str, locale: str, **fmt: Any) -> str:
    subjects = _UPLINK_SUBJECTS[kind]
    template = subjects.get(locale) or subjects["es"]
    return template.format(**fmt)


class EmailService(ABC):
    """Abstract base class for email services"""

    @abstractmethod
    async def send_invitation_email(
        self, to_email: str, invitation_link: str, company_name: str, invited_by: str,
        locale: str = "es",
    ) -> bool:
        """Send invitation email to new user"""

    @abstractmethod
    async def send_welcome_email(
        self, to_email: str, user_name: str, locale: str = "es"
    ) -> bool:
        """Send welcome email after user accepts invitation"""

    @abstractmethod
    async def send_payment_receipt(
        self, to_email: str, invoice_data: Dict[str, Any]
    ) -> bool:
        """Send payment receipt"""

    @abstractmethod
    async def send_confirmation_email(
        self, to_email: str, confirmation_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        """Send signup confirmation email; login is blocked until this link is clicked."""

    @abstractmethod
    async def send_password_reset_email(
        self, to_email: str, reset_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        """Send password reset email with a short-lived reset link."""

    @abstractmethod
    async def send_payment_failed_email(
        self, to_email: str, company_name: str, invoice_data: Dict[str, Any]
    ) -> bool:
        """Send notice that an automatic subscription charge could not be processed."""

    @abstractmethod
    async def send_join_request_decision_email(
        self, to_email: str, user_name: str, company_name: str, approved: bool
    ) -> bool:
        """Notify a join-request requester that an admin approved or rejected them."""


class MockEmailService(EmailService):
    """Mock email service for development/testing - logs to console"""

    async def send_invitation_email(
        self, to_email: str, invitation_link: str, company_name: str, invited_by: str,
        locale: str = "es",
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Invitation sent to {to_email} (locale={locale})\n"
            f"Company: {company_name}\n"
            f"Invited by: {invited_by}\n"
            f"Link: {invitation_link}"
        )
        return True

    async def send_welcome_email(
        self, to_email: str, user_name: str, locale: str = "es"
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Welcome email sent to {to_email} ({user_name}, locale={locale})"
        )
        return True

    async def send_payment_receipt(
        self, to_email: str, invoice_data: Dict[str, Any]
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Payment receipt sent to {to_email}\n"
            f"Invoice: {invoice_data.get('invoice_number')}\n"
            f"Amount: ${invoice_data.get('total', 0) / 100:.2f}"
        )
        return True

    async def send_confirmation_email(
        self, to_email: str, confirmation_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Confirmation email sent to {to_email} ({user_name}, locale={locale})\n"
            f"Link: {confirmation_link}"
        )
        return True

    async def send_password_reset_email(
        self, to_email: str, reset_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Password reset email sent to {to_email} ({user_name}, locale={locale})\n"
            f"Link: {reset_link}"
        )
        return True

    async def send_payment_failed_email(
        self, to_email: str, company_name: str, invoice_data: Dict[str, Any]
    ) -> bool:
        logger.info(
            f"[MOCK EMAIL] Payment failed notice sent to {to_email}\n"
            f"Company: {company_name}\n"
            f"Invoice: {invoice_data.get('invoice_number')}"
        )
        return True

    async def send_join_request_decision_email(
        self, to_email: str, user_name: str, company_name: str, approved: bool
    ) -> bool:
        decision = "approved" if approved else "rejected"
        logger.info(
            f"[MOCK EMAIL] Join request {decision} notice sent to {to_email} ({user_name})\n"
            f"Company: {company_name}"
        )
        return True



class SMTPEmailService(EmailService):
    """Sends real email via direct SMTP (e.g. a Hostinger mailbox)."""

    def __init__(
        self, host: str, port: int, username: str, password: str,
        from_address: str, use_tls: bool = True
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_address = from_address
        self.use_tls = use_tls

    async def _send(self, to_email: str, subject: str, html_body: str) -> bool:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(html_body, subtype="html")

        try:
            await aiosmtplib.send(
                message,
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
            )
            logger.info(f"Email '{subject}' sent to {to_email}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email '{subject}' to {to_email}: {e}")
            return False

    async def send_invitation_email(
        self, to_email: str, invitation_link: str, company_name: str, invited_by: str,
        locale: str = "es",
    ) -> bool:
        html = render_email(
            "invitation.html", locale=locale, invitation_link=invitation_link,
            company_name=company_name, invited_by=invited_by,
        )
        subject = _localized_subject("invitation", locale, company_name=company_name)
        return await self._send(to_email, subject, html)

    async def send_welcome_email(
        self, to_email: str, user_name: str, locale: str = "es"
    ) -> bool:
        html = render_email("welcome.html", locale=locale, user_name=user_name)
        return await self._send(to_email, _localized_subject("welcome", locale), html)

    async def send_payment_receipt(
        self, to_email: str, invoice_data: Dict[str, Any]
    ) -> bool:
        total_formatted = f"${invoice_data.get('total', 0) / 100:.2f}"
        html = render_email(
            "payment_receipt.html",
            company_name=invoice_data.get("company_name", "your company"),
            invoice_number=invoice_data.get("invoice_number"),
            total_formatted=total_formatted,
        )
        return await self._send(to_email, "Payment Receipt", html)

    async def send_confirmation_email(
        self, to_email: str, confirmation_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        html = render_email(
            "confirmation.html", locale=locale, user_name=user_name,
            confirmation_link=confirmation_link,
        )
        return await self._send(to_email, _localized_subject("confirmation", locale), html)

    async def send_password_reset_email(
        self, to_email: str, reset_link: str, user_name: str,
        locale: str = "es",
    ) -> bool:
        html = render_email(
            "password_reset.html", locale=locale, user_name=user_name, reset_link=reset_link
        )
        return await self._send(to_email, _localized_subject("password_reset", locale), html)

    async def send_payment_failed_email(
        self, to_email: str, company_name: str, invoice_data: Dict[str, Any]
    ) -> bool:
        total_formatted = f"${invoice_data.get('total', 0) / 100:.2f}"
        html = render_email(
            "payment_failed.html", company_name=company_name,
            invoice_number=invoice_data.get("invoice_number"), total_formatted=total_formatted,
        )
        return await self._send(to_email, "Payment failed", html)

    async def send_join_request_decision_email(
        self, to_email: str, user_name: str, company_name: str, approved: bool
    ) -> bool:
        html = render_email(
            "join_request_decision.html", user_name=user_name,
            company_name=company_name, approved=approved,
        )
        subject = "Join request approved" if approved else "Join request declined"
        return await self._send(to_email, subject, html)
