"""per-service provisioning parameter values (doc 33 follow-up)

Founder decision 2026-07-21: a service plan can declare a provisioning
parameter whose VALUE is set per subscriber rather than shared. The plan keeps
the declaration (key / description / scope); each ClientService supplies its
own value here. Both reach a playbook as `{{service_plan.<key>}}`, so flipping
a parameter's scope never requires editing a playbook.

Purely additive:
- `client_service.provisioning_params` (JSON, nullable) — the per-service
  values, shape `[{"key": ..., "value": ...}]`.
- No change to `service_plan.provisioning_params`. Its rows gain an optional
  `scope` field, and a row without one is plan-scoped — which is exactly what
  every row written before this feature is, so existing data needs no rewrite.

`connection_params` is deliberately untouched: it remains the free-form
connection long-tail and is not a playbook variable source.

Revision ID: sp1_service_params
Revises: pv1_namespaced_variables
"""
from alembic import op
import sqlalchemy as sa

revision = 'sp1_service_params'
down_revision = 'pv1_namespaced_variables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE client_service ADD COLUMN IF NOT EXISTS provisioning_params JSON"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE client_service DROP COLUMN IF EXISTS provisioning_params"
    )
