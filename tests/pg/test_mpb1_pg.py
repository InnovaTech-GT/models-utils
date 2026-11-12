"""mpb1_managed_playbooks on a real Postgres: the BASELINE data step, the action
CHECK, the staff_actor server default and an idempotent downgrade/upgrade.
Each test runs in one rolled-back transaction."""
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


def _mpb1():
    return load("versions/mpb1_managed_playbooks.py", "mpb1_managed_playbooks")


def _company(conn):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"mpb1-{co}")
    return co


def _playbook(conn, co):
    pb = uuid.uuid4()
    _x(conn, "INSERT INTO playbook (id, created_at, updated_at, name, version, is_active, definition, company_id) "
             "VALUES (:id, now(), now(), 'OLT', 3, true, CAST(:d AS JSON), :co)",
       id=pb, d='{"configuration": []}', co=co)
    return pb


def test_baseline_row_per_existing_playbook(conn):
    m = _mpb1()
    with Operations.context(MigrationContext.configure(conn)):
        m.downgrade()
        pb = _playbook(conn, _company(conn))
        m.upgrade()
    row = _x(conn, "SELECT action, version, definition::text, actor_user_id, actor_staff, playbook_name "
                   "FROM playbook_revision WHERE playbook_id = :p", p=pb).one()
    assert tuple(row) == ("BASELINE", 3, '{"configuration": []}', None, False, "OLT")
    # every playbook got exactly one baseline
    assert _x(conn, "SELECT count(*) FROM playbook").scalar() == \
        _x(conn, "SELECT count(*) FROM playbook_revision WHERE action = 'BASELINE'").scalar()


def test_action_check_rejects_unknown(conn):
    co = _company(conn)
    with pytest.raises(sa.exc.IntegrityError):
        _x(conn, "INSERT INTO playbook_revision (id, company_id, playbook_id, playbook_name, version, action) "
                 "VALUES (:id, :co, :p, 'x', 1, 'RENAME')", id=uuid.uuid4(), co=co, p=uuid.uuid4())


def test_playbook_id_has_no_fk_and_company_cascades(conn):
    co = _company(conn)
    _x(conn, "INSERT INTO playbook_revision (id, company_id, playbook_id, playbook_name, version, action) "
             "VALUES (:id, :co, :p, 'gone', 1, 'DELETE')", id=uuid.uuid4(), co=co, p=uuid.uuid4())
    row = _x(conn, "SELECT actor_staff, created_at IS NOT NULL FROM playbook_revision WHERE company_id = :co",
             co=co).one()
    assert tuple(row) == (False, True)
    _x(conn, "DELETE FROM company WHERE id = :co", co=co)
    assert _x(conn, "SELECT count(*) FROM playbook_revision WHERE company_id = :co", co=co).scalar() == 0


def test_staff_actor_server_default_false(conn):
    co = _company(conn)
    cl, cs, run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)", id=cl, co=co)
    plan = uuid.uuid4()
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, "
             "is_active, company_id) VALUES (:id, now(), now(), 'p', 'FIBER', 1, true, :co)", id=plan, co=co)
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, service_plan_id) "
             "VALUES (:id, now(), now(), :co, :cl, :pl)", id=cs, co=co, cl=cl, pl=plan)
    _x(conn, "INSERT INTO provisioning_run (id, created_at, updated_at, purpose, path, plan, frames, "
             "company_id, client_service_id) VALUES (:id, now(), now(), 'ACTIVATION', '[]', '[]', '{}', :co, :cs)",
       id=run, co=co, cs=cs)
    assert _x(conn, "SELECT staff_actor FROM provisioning_run WHERE id = :r", r=run).scalar() is False


def test_downgrade_then_upgrade_is_clean(conn):
    m = _mpb1()
    cols = ("SELECT count(*) FROM information_schema.columns WHERE (table_name, column_name) IN "
            "(('company', 'playbook_management'), ('provisioning_run', 'staff_actor'))")
    with Operations.context(MigrationContext.configure(conn)):
        m.downgrade()
        m.downgrade()
        assert _x(conn, cols).scalar() == 0
        assert _x(conn, "SELECT to_regclass('playbook_revision')").scalar() is None
        m.upgrade()
        n = _x(conn, "SELECT count(*) FROM playbook_revision").scalar()
        m.upgrade()
    assert _x(conn, cols).scalar() == 2
    assert _x(conn, "SELECT count(*) FROM playbook_revision").scalar() == n
    for ix in ("ix_playbook_revision_company_created", "ix_playbook_revision_playbook"):
        assert _x(conn, "SELECT to_regclass(:i)", i=ix).scalar() == ix


def test_viewer_regranted_read_is_still_denied(conn):
    """A3: after `alembic upgrade head` the convergent seed re-grants the global
    VIEWER role playbooks.read; the overlay still denies it on a managed company."""
    from sqlalchemy.orm import Session

    from database_utils.models.auth import Role, User
    from database_utils.utils.permission_utils import PermissionChecker

    assert _x(conn, "SELECT count(*) FROM role r JOIN role_permission rp ON rp.role_id = r.id "
                    "JOIN permission p ON p.id = rp.permission_id "
                    "WHERE r.name = 'VIEWER' AND r.company_id IS NULL AND p.name = 'playbooks.read'").scalar() == 1
    co, uid = _company(conn), uuid.uuid4()
    _x(conn, "INSERT INTO \"user\" (id, created_at, name, email, age, password_hash, active, email_verified, is_super_admin, "
             "company_id) VALUES (:id, now(), 'v', :e, 30, 'x', true, true, false, :co)", id=uid, e=f"{uid}@e.com", co=co)
    _x(conn, "INSERT INTO user_role (user_id, role_id) SELECT :u, id FROM role "
             "WHERE name = 'VIEWER' AND company_id IS NULL", u=uid)
    db = Session(bind=conn)
    user = PermissionChecker.get_user_by_id_with_roles(db, uid)
    assert user.roles[0].name == "VIEWER"
    assert not PermissionChecker.has_permission(user, "playbooks.read")
    assert PermissionChecker.has_permission(user, "clients.read")
    _x(conn, "UPDATE company SET playbook_management = CAST(:pm AS JSON) WHERE id = :co",
       pm='{"status": "APPROVED"}', co=co)
    db.expire_all()
    assert PermissionChecker.has_permission(PermissionChecker.get_user_by_id_with_roles(db, uid), "playbooks.read")
