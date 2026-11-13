"""Run triggers backfill: past cron suspensions and API runs get their new labels

Revision ID: rtg2_run_trigger_backfill
Revises: rtg1_run_triggers
Create Date: 2026-11-13

Doc 68 §3.4. Data only, no DDL. HELD: this revision is deliberately not part of
the release that carries rtg1. Rewriting rows to a label an old process does not
know makes that process raise LookupError when it loads the row, so it ships
only once production already runs a worker and backend-erp that know the labels
(68-A3). Its down_revision is rewritten to the models-utils head of that moment.

  1. Runs a cron-user (system-cron@internal.erp) SUSPENSION run created as
     USER become AUTO_SUSPENSION. Past run-now suspensions by a tenant admin and
     past payment reactivations are indistinguishable from other USER runs and
     stay USER. Without a cron user in the DB this UPDATE is a no-op.
  2. Every API run becomes BOT (Claude's call, doc 68 §3.2).
  3. A job follows its run (AUTO_SUSPENSION or BOT). Standalone API jobs
     (run_id NULL: connectivity sweeps) are untouched.

Idempotent: a second run finds nothing left to move. DeviceActionLog is
append-only and not touched. Downgrade is a documented no-op (one-way).

Hand-written, house style: lock_timeout, idempotent.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "rtg2_run_trigger_backfill"
down_revision: str | Sequence[str] | None = "rtg1_run_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRON_EMAIL = "system-cron@internal.erp"

STATEMENTS = (
    f"""
    UPDATE provisioning_run r SET triggered_by = 'AUTO_SUSPENSION'
      FROM "user" u
     WHERE r.triggered_by_user_id = u.id AND u.email = '{CRON_EMAIL}'
       AND r.purpose = 'SUSPENSION' AND r.triggered_by = 'USER'
    """,
    "UPDATE provisioning_run SET triggered_by = 'BOT' WHERE triggered_by = 'API'",
    """
    UPDATE provisioning_job j SET triggered_by = r.triggered_by
      FROM provisioning_run r
     WHERE j.run_id = r.id AND r.triggered_by IN ('AUTO_SUSPENSION', 'BOT')
       AND j.triggered_by <> r.triggered_by
    """,
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    for sql in STATEMENTS:
        connection.execute(text(sql))
    print("[rtg2_run_trigger_backfill] upgrade complete")


def downgrade() -> None:
    # Documented no-op: the backfill is one-way (nothing records which rows it moved).
    print("[rtg2_run_trigger_backfill] downgrade is a no-op (one-way data backfill)")
