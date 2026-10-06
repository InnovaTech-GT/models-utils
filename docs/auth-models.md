# Auth Models

## Description

SQLAlchemy ORM models for authentication, authorization, tenancy, email/password
token flows, and the SaaS billing plane (`database_utils/models/auth.py`).

## Goal

Provide a single shared definition for auth-related database tables consumed by
both auth-erp (primary) and backend-erp (token/permission validation).

## Models (in `database_utils/models/auth.py`; table names in parens)

| Model | Purpose |
|-------|---------|
| `Tier` (tier) | SaaS subscription plans (features/modules JSON) |
| `Company` (company) | Multi-tenant ISP company. `mobile_settings` JSON (mi2): `{bank_account, collector_daily_goal, technician_daily_goal}`, validated by `schemas.company.MobileSettings` |
| `User` (user) | Authenticated user (incl. super-admin flag) |
| `Role` (role) | Permission group |
| `Permission` (permission) | Single access right (resource + action) |
| `Notification` (notification) | Pending user **invitation** (despite the name) |
| `UserNotification` (user_notification) | Field-app notification feed (mi2): `kind` in `USER_NOTIFICATION_KINDS` (TASK_ASSIGNED/TASK_OVERDUE/PAYMENTS_OVERDUE, CHECK), `dedupe_key` unique per user, `payload` JSON, `read_at`. Produced lazily by backend-erp when the feed is read |
| `AuditLog` (audit_log) | Immutable audit trail (see `utils/audit_utils.py`) |
| `UserInvitation` (user_invitation) | Invitation flow |
| `EmailVerificationToken` (email_verification_token) | Email verification flow — existing users were grandfathered by the `c1f_verify_grandfather` migration |
| `PasswordResetToken` (password_reset_token) | Password reset flow |
| `RefreshToken` (auth_refresh_token) | Server-side record of every issued refresh token (rt1), PK = the JWT `jti` claim. `family_id` groups a login's rotation chain; `/refresh` sets `rotated_at` + `replaced_by` on the presented row and inserts the successor; presenting a rotated token again (reuse) sets `revoked_at` on the whole family; logout revokes the family. Also `user_id` (FK CASCADE), `company_id` (FK CASCADE, nullable), `client_type` (`web`/`mobile`, CHECK), `issued_at`, `expires_at` (indexed; expired rows are dead and deletable). Logic lives in auth-erp `routers/auth.py` |
| `Subscription` (subscription) | Company's SaaS subscription |
| `PaymentMethod` (payment_method) | SaaS billing payment method |
| `BillingInvoice` (billing_invoice) | SaaS subscription invoice |
| `BillingWebhookEvent` (billing_webhook_event) | Recurrente webhook delivery idempotency log — `svix_id` string PK + `event_type`/`created_at` (rb1) |

Note the two billing domains: these SaaS-billing models cover ISP companies
paying Uplink; **subscriber** billing (ISP end-customers) lives in the CRM/ISP
models ([crm-models.md](crm-models.md), [isp-models.md](isp-models.md)).

`TierChangeRequest` (the manual tier-change approval workflow) was removed —
superseded by Recurrente self-serve checkout/cancel. Its physical
`tier_change_request` table is still present pending a later destructive-change
release (drop-after-prod rule); see [limitations.md](limitations.md).

## Connections to Other Components

- **auth-erp**: primary consumer of all auth models; drives the email flows
  via the shared [email service](email-service.md)
- **backend-erp**: reads User, Company, Role, Permission for auth validation
  (`utils/jwt_utils.py`, `utils/permission_utils.py`)
- **cron-erp**: hits auth-erp's billing endpoints that operate on
  Subscription/BillingInvoice
- **Seeds**: `alembic/seeds/rbac_seed.py` (permissions/roles), `alembic/seeds/tier_seed.py` (importable as `seeds.*` because `env.py` adds the alembic dir to `sys.path`)
- **Auth schemas** ([schemas.md](schemas.md)): Pydantic representations

## Key Implementation Details

- All models use UUID v4 `id` primary keys with `created_at` /
  `updated_at` timestamps
- Foreign keys use `ondelete="CASCADE"` or `SET NULL` as appropriate
- Many-to-many: User ↔ Role and Role ↔ Permission via association tables
- Billing/status fields are **not** Python enums — they are plain
  `Column(String)` fields whose allowed values are documented in inline comments:
  `Subscription.status` (ACTIVE/PAST_DUE/CANCELED/TRIALING),
  `Subscription.billing_type` (AUTOMATIC/MANUAL),
  `Subscription.billing_cycle` (MONTHLY/YEARLY),
  `BillingInvoice.status` (PENDING/PAID/FAILED/REFUNDED). The only enum in this
  area, `NotificationStatus`, is defined in `schemas/notification.py` (a schema),
  not in the auth model.
- System role names live in `constants/roles.py` (`Roles`:
  ADMIN/VIEWER/COLLECTOR/TECHNICIAN — the only global built-ins since
  `rr1_four_builtin_roles`; tenants add custom roles with `company_id` set and
  may not reuse these names). Only the **global** ADMIN role (`company_id IS
  NULL`) gets the `*` wildcard (`PermissionChecker`) or passes
  `get_admin_user` / `require_roles` — name matches on tenant roles never count.
  `web.access` gates the web dashboard (VIEWER + custom roles hold it;
  COLLECTOR/TECHNICIAN are mobile-only). VIEWER holds every `read` permission
  except `device_credentials.read` (`vw1_viewer_no_credential_read`,
  `rbac_seed.VIEWER_PERMISSION_FILTER`)
- **Recurrente gateway columns** (`rb1_recurrente_billing`, additive):
  `Tier.recurrente_product_id`/`recurrente_price_id` (monthly)/
  `recurrente_price_yearly_id` — a NULL price id means the tier is not
  purchasable online; `Company.recurrente_customer_id` — created lazily on
  first checkout; `Subscription.recurrente_subscription_id` (unique),
  `recurrente_checkout_id`, `card_last4`, `card_brand` (display fields);
  `BillingInvoice.recurrente_intent_id` (unique) — webhook charge idempotency.
  `BillingWebhookEvent` provides the second idempotency layer: one row per
  svix delivery id.

## Environment Variables

- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — database connection (via `database.py`)
