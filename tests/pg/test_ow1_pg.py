"""ow1_onu_web on a real Postgres (doc 77): the re-created kind CHECK accepts
CPE_WEB, priority backfills 0, the downgrade refuses while a CPE_WEB credential
exists, and onu_config_backup's FK delete behaviour. Run against a database at
`alembic upgrade head`; each test runs in one rolled-back transaction.
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
    ow1 = load("versions/ow1_onu_web.py", "ow1_onu_web")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(ow1, fn)()


def _columns(conn, table):
    return {r[0] for r in _x(conn, "SELECT column_name FROM information_schema.columns "
                                   "WHERE table_schema = current_schema() AND table_name = :t",
                             t=table)}


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"ow1-{co}")
    return co


def _credential(conn, co, kind):
    cid = uuid.uuid4()
    _x(conn, "INSERT INTO device_credential (id, created_at, updated_at, name, kind, "
             "secret_ciphertext, dek_wrapped, kek_id, company_id) "
             "VALUES (:id, now(), now(), 'c', :k, '\\x00', '\\x00', 'k1', :co)",
       id=cid, k=kind, co=co)
    return cid


def _savepoint_raises(conn, fn):
    sp = conn.begin_nested()
    with pytest.raises((sa.exc.DBAPIError, RuntimeError)):
        fn()
    sp.rollback()


def _backup(conn, co, item, run=None, job=None, cred=None):
    bid = uuid.uuid4()
    _x(conn, "INSERT INTO onu_config_backup (id, company_id, inventory_item_id, "
             "provisioning_run_id, provisioning_job_id, credential_id, sha256, size_bytes, "
             "secret_ciphertext, dek_wrapped, kek_id) "
             "VALUES (:id, :co, :it, :r, :j, :c, repeat('0', 64), 1, '\\x00', '\\x00', 'k1')",
       id=bid, co=co, it=item, r=run, j=job, c=cred)
    return bid


def test_cpe_web_accepted_and_priority_defaults_to_zero(conn):
    co = _company(conn)
    cid = _credential(conn, co, "CPE_WEB")
    assert _x(conn, "SELECT priority FROM device_credential WHERE id = :i", i=cid).scalar() == 0
    _savepoint_raises(conn, lambda: _credential(conn, co, "NOT_A_KIND"))


def test_upgrade_backfills_priority_on_existing_rows(conn):
    _x(conn, "DELETE FROM device_credential WHERE kind = 'CPE_WEB'")
    _run(conn, "downgrade")
    assert "priority" not in _columns(conn, "device_credential")
    cid = _credential(conn, _company(conn), "SSH")
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _x(conn, "SELECT priority FROM device_credential WHERE id = :i", i=cid).scalar() == 0
    assert "cpe_web_cidrs" in _columns(conn, "provisioning_settings")


def test_downgrade_refuses_while_a_cpe_web_credential_exists(conn):
    _credential(conn, _company(conn), "CPE_WEB")
    _savepoint_raises(conn, lambda: _run(conn, "downgrade"))
    _x(conn, "DELETE FROM device_credential WHERE kind = 'CPE_WEB'")
    _run(conn, "downgrade")
    assert not _columns(conn, "onu_config_backup")
    assert "cpe_web_cidrs" not in _columns(conn, "provisioning_settings")
    _savepoint_raises(conn, lambda: _credential(conn, _company(conn), "CPE_WEB"))


def _fixture_fks(conn):
    """One company with an ONU item, a run and a job on it."""
    co = _company(conn)
    client, plan, svc, dtype, item, pb, run, job = (uuid.uuid4() for _ in range(8))
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) "
             "VALUES (:id, now(), 'ow1', :co)", id=client, co=co)
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, price, is_active, "
             "plan_type, company_id) VALUES (:id, now(), now(), 'ow1', 1, true, 'FIBER', :co)",
       id=plan, co=co)
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
             "service_plan_id) VALUES (:id, now(), now(), :co, :cl, :p)",
       id=svc, co=co, cl=client, p=plan)
    _x(conn, "INSERT INTO device_type (id, created_at, updated_at, name, company_id, category_id) "
             "SELECT :id, now(), now(), 'ow1', :co, id FROM device_category LIMIT 1",
       id=dtype, co=co)
    _x(conn, "INSERT INTO inventory_item (id, created_at, updated_at, company_id, "
             "device_type_id, network_attached) VALUES (:id, now(), now(), :co, :t, true)",
       id=item, co=co, t=dtype)
    _x(conn, "INSERT INTO playbook (id, created_at, updated_at, company_id, name, definition, "
             "is_active, version, last_dry_run_version) VALUES (:id, now(), now(), :co, 'ow1', "
             "'{}', true, 1, 1)", id=pb, co=co)
    _x(conn, "INSERT INTO provisioning_run (id, created_at, updated_at, company_id, "
             "client_service_id, purpose, path, plan, frames) "
             "VALUES (:id, now(), now(), :co, :s, 'MIGRATION', '[]', '[]', '[]')",
       id=run, co=co, s=svc)
    _x(conn, "INSERT INTO provisioning_job (id, created_at, attempts, max_attempts, company_id, "
             "playbook_id, inventory_item_id, run_id) "
             "VALUES (:id, now(), 0, 3, :co, :pb, :it, :r)",
       id=job, co=co, pb=pb, it=item, r=run)
    return co, item, run, job


def test_backup_fks_set_null_on_run_job_credential(conn):
    co, item, run, job = _fixture_fks(conn)
    cred = _credential(conn, co, "CPE_WEB")
    bid = _backup(conn, co, item, run, job, cred)
    _x(conn, "DELETE FROM device_credential WHERE id = :i", i=cred)
    _x(conn, "DELETE FROM provisioning_job WHERE run_id = :r", r=run)
    _x(conn, "DELETE FROM provisioning_run WHERE id = :r", r=run)
    row = _x(conn, "SELECT provisioning_run_id, provisioning_job_id, credential_id "
                   "FROM onu_config_backup WHERE id = :i", i=bid).one()
    assert tuple(row) == (None, None, None)


def test_backup_cascades_with_the_item(conn):
    co, item, _run_id, _job = _fixture_fks(conn)
    bid = _backup(conn, co, item)
    _x(conn, "DELETE FROM provisioning_job WHERE inventory_item_id = :i", i=item)
    _x(conn, "DELETE FROM inventory_item WHERE id = :i", i=item)
    assert _x(conn, "SELECT count(*) FROM onu_config_backup WHERE id = :i", i=bid).scalar() == 0
