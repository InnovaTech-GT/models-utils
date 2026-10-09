"""manual playbook steps: PENDING_MANUAL job status, in-flight indexes, ZTP_MANUAL_STEP kind

Revision ID: zm1_manual_step
Revises: zt1_ztp_trigger
Create Date: 2026-10-09

Doc 42d §7. Additive:

provisioningjobstatus      + 'PENDING_MANUAL' (ALTER TYPE ... ADD VALUE in an
                           autocommit block, the nc1a recipe: the value must be
                           committed before the index predicates below use it)
provisioning_job           ~ uq_provisioning_job_company_idem, uq_provisioning_job_device_lock
provisioning_run           ~ uq_provisioning_run_company_idem
                           (in-flight predicate + PENDING_MANUAL; a partial index
                           predicate cannot be altered, so drop + re-create)
user_notification          ~ ck_user_notification_kind + 'ZTP_MANUAL_STEP'

Downgrade refuses while any job is PENDING_MANUAL (the old predicates would
release its device lock and dedupe key), then restores the old predicates,
deletes the ZTP_MANUAL_STEP rows and the old CHECK. The enum value stays: PG
cannot drop it without rebuilding the type, and nothing reads it once the code
is gone.

The literals are this revision's own copies (revisions are immutable);
tests/test_manual_steps.py pins them to the models and to IN_FLIGHT.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "zm1_manual_step"
down_revision: Union[str, Sequence[str], None] = "zt1_ztp_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IN_FLIGHT = "status IN ('QUEUED','RUNNING','PENDING_INFORM','PENDING_MANUAL')"
_IN_FLIGHT_PRE = "status IN ('QUEUED','RUNNING','PENDING_INFORM')"
_KIND_CHECK = (
    "kind IN ('TASK_ASSIGNED','TASK_OVERDUE','PAYMENTS_OVERDUE',"
    "'ZTP_SUCCEEDED','ZTP_FAILED','ZTP_NEEDS_ATTENTION','ZTP_ROLLBACK_INCOMPLETE',"
    "'ZTP_MANUAL_STEP')"
)
_KIND_CHECK_PRE = (
    "kind IN ('TASK_ASSIGNED','TASK_OVERDUE','PAYMENTS_OVERDUE',"
    "'ZTP_SUCCEEDED','ZTP_FAILED','ZTP_NEEDS_ATTENTION','ZTP_ROLLBACK_INCOMPLETE')"
)
# (index, table, columns, extra predicate)
_INDEXES = (
    ("uq_provisioning_job_company_idem", "provisioning_job", "company_id, idempotency_key",
     "idempotency_key IS NOT NULL"),
    ("uq_provisioning_job_device_lock", "provisioning_job", "device_lock_key",
     "device_lock_key IS NOT NULL"),
    ("uq_provisioning_run_company_idem", "provisioning_run", "company_id, idempotency_key",
     "idempotency_key IS NOT NULL"),
)


def _indexes(c, in_flight: str) -> None:
    for name, table, columns, where in _INDEXES:
        c.execute(text(f"DROP INDEX IF EXISTS {name}"))
        c.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({columns}) "
                       f"WHERE {where} AND {in_flight}"))


def _kind_check(c, literal: str) -> None:
    c.execute(text(
        "ALTER TABLE user_notification DROP CONSTRAINT IF EXISTS ck_user_notification_kind"
    ))
    c.execute(text(
        f"ALTER TABLE user_notification ADD CONSTRAINT ck_user_notification_kind CHECK ({literal})"
    ))


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE provisioningjobstatus ADD VALUE IF NOT EXISTS 'PENDING_MANUAL'")
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    _indexes(c, _IN_FLIGHT)
    _kind_check(c, _KIND_CHECK)


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    parked = c.execute(text(
        "SELECT count(*) FROM provisioning_job WHERE status = 'PENDING_MANUAL'"
    )).scalar()
    if parked:
        raise RuntimeError(
            f"zm1 downgrade refused: {parked} job(s) are PENDING_MANUAL; cancel them or let "
            "them expire first (doc 42d §14)"
        )
    _indexes(c, _IN_FLIGHT_PRE)
    c.execute(text("DELETE FROM user_notification WHERE kind = 'ZTP_MANUAL_STEP'"))
    _kind_check(c, _KIND_CHECK_PRE)
