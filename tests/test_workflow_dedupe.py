"""Cycle 7 (doc 25 §6.3): the workflow engine's duplicate-enqueue pre-check
must treat a PENDING_INFORM (parked) job as in-flight — nc1a extended the
uq_provisioning_job_company_idem partial-index predicate to include it, and a
pre-check narrower than the index predicate would let a re-enqueue fall
through to the INSERT and trip the unique index (failing the step instead of
deduping)."""
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database_utils.database import Base
from database_utils.models.isp import ProvisioningJob, ProvisioningJobStatus
from database_utils.utils.workflow_engine import _find_queued_or_running_provisioning_job

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine)

# Scope create_all to just provisioning_job (test_models_email_tokens.py
# precedent — Base.metadata carries Postgres-only DDL sqlite cannot parse).
# SQLite tolerates the dangling FK references (not enforced without the
# foreign_keys pragma) and ignores the postgresql_where index predicates.
_TEST_TABLES = [ProvisioningJob.__table__]


def _job(db, status, idempotency_key):
    job = ProvisioningJob(
        company_id=uuid.uuid4(),
        playbook_id=uuid.uuid4(),
        status=status,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    db.flush()
    return job


def _run(status, should_dedupe):
    Base.metadata.create_all(bind=engine, tables=_TEST_TABLES)
    db = TestingSession()
    try:
        key = f"activate-{uuid.uuid4()}"
        job = _job(db, status, key)
        found = _find_queued_or_running_provisioning_job(db, job.company_id, key)
        if should_dedupe:
            assert found is not None and found.id == job.id
        else:
            assert found is None
    finally:
        db.rollback()
        Base.metadata.drop_all(bind=engine, tables=_TEST_TABLES)
        db.close()


def test_pending_inform_job_dedupes_reenqueue():
    _run(ProvisioningJobStatus.PENDING_INFORM, should_dedupe=True)


def test_queued_job_dedupes_reenqueue():
    _run(ProvisioningJobStatus.QUEUED, should_dedupe=True)


def test_running_job_dedupes_reenqueue():
    _run(ProvisioningJobStatus.RUNNING, should_dedupe=True)


def test_terminal_job_does_not_dedupe():
    # A finished job must NOT block a fresh enqueue with the same key.
    _run(ProvisioningJobStatus.SUCCEEDED, should_dedupe=False)
