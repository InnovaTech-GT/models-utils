"""pe1_playbook_phases on a real Postgres (doc 42 §12): the phase CHECKs, the
CLI_ENABLE credential kind, and a downgrade that refuses while a CLI_ENABLE
credential exists. Run against a database already at `alembic upgrade head`;
each test runs in one rolled-back transaction (test_port_labels_pg precedent).
"""
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


def _run(conn, fn):
    pe1 = load("versions/pe1_playbook_phases.py", "pe1_playbook_phases")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(pe1, fn)()


def _columns(conn, table):
    return {r[0] for r in _x(conn, "SELECT column_name FROM information_schema.columns "
                                   "WHERE table_schema = current_schema() AND table_name = :t",
                             t=table)}


def _credential(conn, kind):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"pe1-{co}")
    _x(conn, "INSERT INTO device_credential (id, created_at, updated_at, name, kind, "
             "secret_ciphertext, dek_wrapped, kek_id, company_id) "
             "VALUES (:id, now(), now(), 'enable', :k, '\\x00', '\\x00', 'k1', :co)",
       id=uuid.uuid4(), k=kind, co=co)


def _savepoint_raises(conn, fn):
    sp = conn.begin_nested()
    with pytest.raises((sa.exc.DBAPIError, RuntimeError)):
        fn()
    sp.rollback()


def test_head_has_the_columns(conn):
    assert {"phase", "error_code", "error", "outputs", "secrets_ciphertext",
            "secrets_dek_wrapped", "secrets_kek_id"} <= _columns(conn, "provisioning_run")
    assert "phase" in _columns(conn, "provisioning_job")


def test_phase_checks(conn):
    for table in ("provisioning_run", "provisioning_job"):
        definition = _x(conn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                              "WHERE conname = :n", n=f"ck_{table}_phase").scalar()
        assert definition and "'ROLLBACK'" in definition and "IS NULL" in definition


def test_cli_enable_is_accepted_and_blocks_the_downgrade(conn):
    _credential(conn, "CLI_ENABLE")
    _savepoint_raises(conn, lambda: _credential(conn, "NOT_A_KIND"))
    _savepoint_raises(conn, lambda: _run(conn, "downgrade"))


def test_downgrade_then_upgrade(conn):
    _x(conn, "DELETE FROM device_credential WHERE kind = 'CLI_ENABLE'")
    _run(conn, "downgrade")
    assert "phase" not in _columns(conn, "provisioning_run")
    assert "phase" not in _columns(conn, "provisioning_job")
    _savepoint_raises(conn, lambda: _credential(conn, "CLI_ENABLE"))
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    _credential(conn, "CLI_ENABLE")
    assert "secrets_kek_id" in _columns(conn, "provisioning_run")
