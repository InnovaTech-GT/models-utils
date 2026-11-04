"""wf2_conversational on a real Postgres at head: the new columns and their
defaults, the C1 partial unique (one WAITING run per awaited_key), the
messages_sent partial index, and an idempotent downgrade/upgrade. Each test
runs in one rolled-back transaction."""
import os
import uuid

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

KEY = "wa:co:int:50255550000"


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


def _workflow(conn):
    co, wf = uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"wf2-{co}")
    _x(conn, "INSERT INTO workflow (id, created_at, updated_at, name, is_active, company_id, engine_version) "
             "VALUES (:id, now(), now(), 'wf', true, :co, 2)", id=wf, co=co)
    return wf


def _run(conn, wf, status, key=KEY):
    rid = uuid.uuid4()
    _x(conn, "INSERT INTO workflow_execution (id, created_at, status, trigger_event, workflow_id, awaited_key) "
             "VALUES (:id, now(), :s, '{}', :w, :k)", id=rid, s=status, w=wf, k=key)
    return rid


def test_columns_defaults_and_indexes(conn):
    wf = _workflow(conn)
    rid = _run(conn, wf, "PENDING", None)
    row = _x(conn, "SELECT outbox_delivered, awaited_key, cancel_requested_at, cancel_code "
                   "FROM workflow_execution WHERE id = :r", r=rid).one()
    assert tuple(row) == (0, None, None, None)
    sid = uuid.uuid4()
    _x(conn, "INSERT INTO workflow_step_execution (id, created_at, status, execution_id) "
             "VALUES (:id, now(), 'PENDING', :r)", id=sid, r=rid)
    assert _x(conn, "SELECT messages_sent FROM workflow_step_execution WHERE id = :s", s=sid).scalar() == 0
    idx = dict(_x(conn, "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                        "('uq_workflow_execution_awaited', 'ix_step_execution_sent')").all())
    assert "UNIQUE" in idx["uq_workflow_execution_awaited"] and "WAITING" in idx["uq_workflow_execution_awaited"]
    assert "messages_sent > 0" in idx["ix_step_execution_sent"]


def test_one_waiting_run_per_chat(conn):
    wf = _workflow(conn)
    first = _run(conn, wf, "WAITING")
    _run(conn, wf, "CANCELLED")
    _run(conn, wf, "RUNNING")
    _run(conn, wf, "WAITING", None)
    _run(conn, wf, "WAITING", None)
    _run(conn, wf, "WAITING", "test:other")
    with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
        _run(conn, wf, "WAITING")
    # Superseding the first frees the key.
    _x(conn, "UPDATE workflow_execution SET status = 'CANCELLED', error_code = 'SUPERSEDED', awaited_key = NULL "
             "WHERE id = :r", r=first)
    _run(conn, wf, "WAITING")


def test_downgrade_then_upgrade_is_idempotent(conn):
    wf2 = load("versions/wf2_conversational.py", "wf2_conversational")
    cols = ("SELECT count(*) FROM information_schema.columns WHERE (table_name, column_name) IN "
            "(('workflow_execution', 'awaited_key'), ('workflow_execution', 'cancel_requested_at'), "
            "('workflow_execution', 'cancel_code'), ('workflow_execution', 'outbox_delivered'), "
            "('workflow_step_execution', 'messages_sent'))")
    with Operations.context(MigrationContext.configure(conn)):
        wf2.downgrade()
        wf2.downgrade()
        assert _x(conn, cols).scalar() == 0
        assert _x(conn, "SELECT to_regclass('uq_workflow_execution_awaited')").scalar() is None
        wf2.upgrade()
        wf2.upgrade()
    assert _x(conn, cols).scalar() == 5
    assert _x(conn, "SELECT to_regclass('ix_step_execution_sent')").scalar() == "ix_step_execution_sent"
