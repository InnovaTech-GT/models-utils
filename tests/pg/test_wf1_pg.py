"""wf1_workflow_engine_v2 on a real Postgres at head: the new enum labels, the
CHECKs and partial uniques, the workflow <-> version FK cycle (SET NULL both
ways), the ORM's single install flush, cascades, and an idempotent
downgrade/upgrade. Each test runs in one rolled-back transaction."""
import contextlib
import os
import uuid

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

DEF = '{"schema": 1, "trigger": {"type": "event", "key": "task.created"}, "nodes": [], "edges": []}'


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _refused(conn, fn, exc=sa.exc.IntegrityError):
    with pytest.raises(exc), conn.begin_nested():
        fn()


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"wf1-{co}")
    return co


def _workflow(conn, co, engine=2):
    wf = uuid.uuid4()
    _x(conn, "INSERT INTO workflow (id, created_at, updated_at, name, is_active, company_id, engine_version) "
             "VALUES (:id, now(), now(), 'wf', true, :co, :e)", id=wf, co=co, e=engine)
    return wf


def _version(conn, co, wf, number=1, status="PUBLISHED", trigger_type="event"):
    v = uuid.uuid4()
    _x(conn, "INSERT INTO workflow_version (id, workflow_id, company_id, number, status, definition, "
             "trigger_type, trigger_key) VALUES (:id, :wf, :co, :n, :s, CAST(:d AS jsonb), :tt, 'task.created')",
       id=v, wf=wf, co=co, n=number, s=status, d=DEF, tt=trigger_type)
    return v


def _service(conn, co):
    cl, plan, cs = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)", id=cl, co=co)
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, price, is_active, plan_type, "
             "company_id) VALUES (:id, now(), now(), 'p', 1, true, 'FIBER', :co)", id=plan, co=co)
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, service_plan_id) "
             "VALUES (:id, now(), now(), :co, :cl, :p)", id=cs, co=co, cl=cl, p=plan)
    return cs


def _boost(conn, co, cs, status="ACTIVE", **kw):
    row = {"extra_mb": 10, "end_reason": None, **kw}
    _x(conn, "INSERT INTO service_boost (id, company_id, client_service_id, extra_mb, starts_at, expires_at, "
             "status, end_reason) VALUES (:id, :co, :cs, :mb, now(), now() + interval '1 day', :s, :r)",
       id=uuid.uuid4(), co=co, cs=cs, mb=row["extra_mb"], s=status, r=row["end_reason"])


def test_head_has_enum_labels_tables_and_indexes(conn):
    labels = set(_x(conn, "SELECT unnest(enum_range(NULL::executionstatus))::text").scalars())
    assert {"WAITING", "CANCELLED"} <= labels
    for t in ("workflow_version", "automation_template", "workflow_event", "service_boost"):
        assert _x(conn, "SELECT to_regclass(:t)", t=t).scalar() == t
    idx = set(_x(conn, "SELECT indexname FROM pg_indexes WHERE tablename IN "
                       "('workflow_version', 'workflow_event', 'workflow_execution', "
                       "'workflow_step_execution', 'service_boost')").scalars())
    assert {"uq_workflow_version_draft", "ix_workflow_version_trigger", "ix_workflow_event_pending",
            "uq_workflow_execution_dedupe", "ix_workflow_execution_runnable", "uq_step_execution_node",
            "ix_step_execution_messages", "uq_service_boost_open", "ix_service_boost_due"} <= idx


def test_version_rules(conn):
    co = _company(conn)
    wf = _workflow(conn, co)
    _version(conn, co, wf, 1, "PUBLISHED")
    _version(conn, co, wf, 2, "DRAFT")
    _refused(conn, lambda: _version(conn, co, wf, 3, "DRAFT"))        # one draft
    _refused(conn, lambda: _version(conn, co, wf, 1, "SUPERSEDED"))   # number unique
    _refused(conn, lambda: _version(conn, co, wf, 4, "ARCHIVED"))
    _refused(conn, lambda: _version(conn, co, wf, 5, trigger_type="cron"))
    _refused(conn, lambda: _x(conn, "UPDATE workflow SET engine_version = 3 WHERE id = :w", w=wf))


def test_version_delete_nulls_active_and_run_pointers_workflow_delete_cascades(conn):
    co = _company(conn)
    wf = _workflow(conn, co)
    v = _version(conn, co, wf)
    _x(conn, "UPDATE workflow SET active_version_id = :v WHERE id = :w", v=v, w=wf)
    run = uuid.uuid4()
    _x(conn, "INSERT INTO workflow_execution (id, created_at, status, trigger_event, workflow_id, company_id, "
             "version_id, definition) VALUES (:id, now(), 'WAITING', '{}', :w, :co, :v, '{}')",
       id=run, w=wf, co=co, v=v)
    _x(conn, "DELETE FROM workflow_version WHERE id = :v", v=v)
    assert _x(conn, "SELECT active_version_id FROM workflow WHERE id = :w", w=wf).scalar() is None
    assert _x(conn, "SELECT version_id FROM workflow_execution WHERE id = :r", r=run).scalar() is None
    _version(conn, co, wf, 2)
    _x(conn, "DELETE FROM workflow WHERE id = :w", w=wf)
    assert _x(conn, "SELECT count(*) FROM workflow_version WHERE workflow_id = :w", w=wf).scalar() == 0


def test_run_dedupe_and_step_node_unique(conn):
    co = _company(conn)
    wf = _workflow(conn, co)

    def run(key):
        rid = uuid.uuid4()
        _x(conn, "INSERT INTO workflow_execution (id, created_at, status, trigger_event, workflow_id, dedupe_key) "
                 "VALUES (:id, now(), 'PENDING', '{}', :w, :k)", id=rid, w=wf, k=key)
        return rid

    run(None)
    run(None)
    rid = run("ev:1")
    _refused(conn, lambda: run("ev:1"))

    def step():
        _x(conn, "INSERT INTO workflow_step_execution (id, created_at, status, execution_id, node_key) "
                 "VALUES (:id, now(), 'PENDING', :r, 'a')", id=uuid.uuid4(), r=rid)

    step()
    _refused(conn, step)


def test_boost_rules(conn):
    co = _company(conn)
    cs = _service(conn, co)
    _boost(conn, co, cs, "ENDED", end_reason="EXPIRED")
    _boost(conn, co, cs, "FAILED", end_reason="START_FAILED")
    _boost(conn, co, cs, "STARTING")
    for status in ("ACTIVE", "ENDING", "END_FAILED"):
        _refused(conn, lambda s=status: _boost(conn, co, cs, s))  # one open boost per service
    other = _service(conn, co)
    _refused(conn, lambda: _boost(conn, co, other, extra_mb=0))
    _refused(conn, lambda: _boost(conn, co, other, "PAUSED"))
    _refused(conn, lambda: _boost(conn, co, other, end_reason="BORED"))
    _x(conn, "DELETE FROM client_service WHERE id = :cs", cs=cs)
    assert _x(conn, "SELECT count(*) FROM service_boost WHERE client_service_id = :cs", cs=cs).scalar() == 0


def test_backfilled_columns_and_outbox_defaults(conn):
    co = _company(conn)
    eid = _x(conn, "INSERT INTO workflow_event (company_id, name, payload) VALUES (:co, 'task.created', "
                   "'{}'::jsonb) RETURNING id", co=co).scalar()
    row = _x(conn, "SELECT depth, attempts, processed_at, created_at FROM workflow_event WHERE id = :i", i=eid).one()
    assert row[0] == 0 and row[1] == 0 and row[2] is None and row[3] is not None
    cols = dict(_x(conn, "SELECT table_name || '.' || column_name, is_nullable FROM information_schema.columns "
                         "WHERE (table_name, column_name) IN (('company', 'workflow_daily_message_cap'), "
                         "('provisioning_run', 'workflow_execution_id'), ('workflow', 'engine_version'))").all())
    assert cols == {"company.workflow_daily_message_cap": "YES",
                    "provisioning_run.workflow_execution_id": "YES", "workflow.engine_version": "NO"}


def test_orm_install_flush_on_postgres(conn):
    from database_utils.models import Workflow, WorkflowVersion, WorkflowVersionStatus
    co = _company(conn)
    with Session(bind=conn, join_transaction_mode="create_savepoint") as db:
        wf = Workflow(name="install", company_id=co, engine_version=2)
        v1 = WorkflowVersion(workflow=wf, company_id=co, number=1, status=WorkflowVersionStatus.PUBLISHED,
                             definition={"schema": 1}, trigger_type="event", trigger_key="task.created")
        wf.active_version = v1
        db.add(wf)
        db.flush()
        assert _x(conn, "SELECT active_version_id FROM workflow WHERE id = :w", w=wf.id).scalar() == v1.id


def test_downgrade_then_upgrade_is_idempotent(conn, monkeypatch):
    wf1 = load("versions/wf1_workflow_engine_v2.py", "wf1_workflow_engine_v2")
    # ADD VALUE IF NOT EXISTS of an existing label is a no-op inside a transaction.
    monkeypatch.setattr(MigrationContext, "autocommit_block", lambda self: contextlib.nullcontext())
    with Operations.context(MigrationContext.configure(conn)):
        wf1.downgrade()
        wf1.downgrade()
        assert _x(conn, "SELECT to_regclass('workflow_version')").scalar() is None
        wf1.upgrade()
        wf1.upgrade()
    assert _x(conn, "SELECT to_regclass('workflow_version')").scalar() == "workflow_version"
    assert _x(conn, "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_workflow_active_version'").scalar() == 1
