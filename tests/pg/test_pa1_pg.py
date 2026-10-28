"""pa1_plan_network_automation on a real Postgres, at `alembic upgrade head`:
every SERVICE plan holds ONU, a plan inserted without the flag is automated,
the category FK refuses a delete in use, ADMIN holds client_services.cancel and
RECONNECT is a taskjobkind label. Each test runs in one rolled-back transaction."""
import os
import uuid

import pytest
import sqlalchemy as sa

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


def test_backfill_left_no_service_plan_without_onu(conn):
    assert _x(conn, "SELECT count(*) FROM service_plan sp WHERE sp.kind = 'SERVICE' AND NOT EXISTS ("
                    "SELECT 1 FROM service_plan_device_category spc JOIN device_category dc "
                    "ON dc.id = spc.device_category_id WHERE spc.service_plan_id = sp.id AND dc.key = 'ONU')"
              ).scalar() == 0


def test_plan_defaults_automated_and_category_delete_restricted(conn):
    co, plan, cat = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"pa1-{co}")
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, "
             "is_active, company_id) VALUES (:id, now(), now(), 'p', 'FIBER', 1, true, :co)", id=plan, co=co)
    assert _x(conn, "SELECT network_automation FROM service_plan WHERE id = :id", id=plan).scalar() is True
    _x(conn, "INSERT INTO device_category (id, created_at, updated_at, key, name) VALUES (:id, now(), now(), :k, 'x')", id=cat, k=f"PA1-{cat.hex[:8]}")
    _x(conn, "INSERT INTO service_plan_device_category VALUES (:p, :c)", p=plan, c=cat)
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _x(conn, "DELETE FROM device_category WHERE id = :id", id=cat)
    _x(conn, "DELETE FROM service_plan WHERE id = :id", id=plan)
    assert _x(conn, "SELECT count(*) FROM service_plan_device_category WHERE device_category_id = :c",
              c=cat).scalar() == 0


def test_cancel_admin_only_and_reconnect_label(conn):
    holders = _x(conn, "SELECT r.name FROM role r JOIN role_permission rp ON rp.role_id = r.id "
                       "JOIN permission p ON p.id = rp.permission_id "
                       "WHERE p.name = 'client_services.cancel' AND r.company_id IS NULL").scalars().all()
    assert holders == ["ADMIN"]
    assert _x(conn, "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'taskjobkind' AND e.enumlabel = 'RECONNECT'").scalar() == 1
