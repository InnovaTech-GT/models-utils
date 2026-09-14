# Email Service

## Description

Transactional email for the platform, shipped as part of this library so any
consumer (primarily `auth-erp`) sends identical, branded emails.

- `database_utils/services/email_service.py` — abstract `EmailService` base +
  `MockEmailService` (logs) + `SMTPEmailService` (aiosmtplib), plus the
  localized Uplink subject dictionary (`_UPLINK_SUBJECTS`)
- `database_utils/utils/email_templates.py` — locale-aware Jinja2 rendering
  helper `render_email(template_name, locale="es", **context)`
- `database_utils/templates/email/*.html` — Jinja2 HTML templates (Uplink
  shell + es/en pairs for the auth emails, single legacy templates for the
  payment/join-request emails)

## Email kinds

The abstract `EmailService` defines seven transactional email kinds:

1. Invitation *(localized es/en)*
2. Welcome *(localized es/en)*
3. Payment receipt
4. Confirmation (email verification) *(localized es/en)*
5. Password reset *(localized es/en)*
6. Payment failed
7. Join-request decision

The four auth-flow methods (`send_confirmation_email`,
`send_invitation_email`, `send_password_reset_email`, `send_welcome_email`)
take `locale: str = "es"` as their **last keyword parameter** — uniformly on
the ABC, `MockEmailService` (which logs the locale), and `SMTPEmailService`
(which selects the localized subject and passes `locale` to `render_email`).
Subjects live in the module-level `_UPLINK_SUBJECTS` dict (es/en per kind,
e.g. `"Confirma tu correo — Uplink"`); unknown locales fall back to Spanish.
Payment/join-request emails are unchanged (English-only).

## Templates (`database_utils/templates/email/`)

Locale resolution in `render_email`: for `confirmation.html` + `locale="en"`
it tries `confirmation.en.html` → `confirmation.es.html` (default locale) →
`confirmation.html`. Templates without locale variants resolve unchanged via
the last step.

| Template | Used for |
|---|---|
| `base_uplink.html` | Uplink shell for the localized auth emails: table-based, max-520px white card on `#f4f4f5`, bold `#1e1b4b` heading, `#374151` body, centered indigo `#6366F1` CTA button, URL-fallback paragraph, `<hr>`, gray footer |
| `confirmation.{es,en}.html` | Email verification (link valid 24 h) |
| `invitation.{es,en}.html` | User invitation (`invited_by` + `company_name`, expires in 7 days) |
| `password_reset.{es,en}.html` | Password reset link (valid 1 hour) |
| `welcome.{es,en}.html` | Welcome email after invitation accept |
| `base_layout.html` | Legacy shared layout wrapper (payment/join-request emails) |
| `join_request_decision.html` | Join-request accept/reject notice (vestigial flow) |
| `payment_failed.html` | Failed payment notice |
| `payment_receipt.html` | Payment receipt |

The single-language `confirmation.html` / `invitation.html` /
`password_reset.html` / `welcome.html` templates were **deleted** in the auth
overhaul — callers keep passing those names and the locale resolution picks
the es/en variant.

Templates are packaged with the wheel via `[options.package_data]` in
`setup.cfg` (`templates/email/*.html` — the glob covers the `.es.html` /
`.en.html` variants). If you add a template, confirm it ships in the built
package.

## Configuration

| Env var | Purpose |
|---|---|
| `EMAIL_PROVIDER` | Selects the provider implementation (`mock` default / `smtp`) |
| `SMTP_USE_TLS` | TLS toggle for the SMTP transport (implicit TLS, port 465 style) |
| SMTP host / credentials | Supplied via the consuming service's environment (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`) |

## Connections to Other Components

- **auth-erp**: primary consumer — invitation, verification
  (`EmailVerificationToken`), and password reset (`PasswordResetToken`) flows;
  routers pass the request's `locale` through to the send methods; see
  [auth-models.md](auth-models.md)
- **Schemas**: `schemas/email_verification.py`, `schemas/password_reset.py`,
  `schemas/invitation.py`, `schemas/requests.py` — the auth request schemas
  carry `locale: Literal["es","en"] = "es"` so the emails land in the user's
  language
- **Token helpers**: `utils/token_utils.py`

## Tests

Email schemas, service, templates (both locales + fallback), SMTP behavior,
and model tokens are covered by ~20 tests in `tests/`.
