"""sm1_snmp_traffic_metrics on a real Postgres: pppoe_username backfill and
uniqueness, traffic_5m daily partitions (routing, missing partition, helpers,
drop), CHECKs, cascades, and an idempotent downgrade/upgrade. Each test runs in
one rolled-back transaction."""
import json
import os
import uuid
from datetime import date, datetime, timedelta

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

from database_utils.utils import traffic_partitions as tp
from database_utils.utils.timezone_utils import GUATEMALA_TZ

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

INSERT_5M = (
    "INSERT INTO traffic_5m (client_service_id, company_id, bucket_start, down_avg_bps, "
    "down_max_bps, up_avg_bps, up_max_bps, samples) VALUES (:s, :co, :b, :da, :dm, 1, 2, :n)"
)


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


def _company(conn):
    co, cl, plan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"sm1-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)",
       id=cl, co=co)
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, "
             "is_active, company_id) VALUES (:id, now(), now(), 'p', 'FIBER', 1, true, :co)",
       id=plan, co=co)
    return co, cl, plan


def _service(conn, co, cl, plan, created="now()", status="ACTIVE", conn_params=None, prov=None, user=None):
    svc = uuid.uuid4()
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
             f"service_plan_id, status, connection_params, provisioning_params, pppoe_username) "
             f"VALUES (:id, {created}, now(), :co, :cl, :pl, :st, "
             "CAST(:cp AS json), CAST(:pp AS json), :u)",
       id=svc, co=co, cl=cl, pl=plan, st=status,
       cp=json.dumps(conn_params) if conn_params is not None else None,
       pp=json.dumps(prov) if prov is not None else None, u=user)
    return svc


def _today():
    return datetime.now(GUATEMALA_TZ).date()


def _user(conn, svc):
    return _x(conn, "SELECT pppoe_username FROM client_service WHERE id = :i", i=svc).scalar()


def test_backfill_from_both_param_shapes_and_duplicates(conn):
    co, cl, plan = _company(conn)
    a = _service(conn, co, cl, plan, conn_params={"pppoe_user": " ana "})
    b = _service(conn, co, cl, plan, prov=[{"key": "vlan", "value": "10"},
                                           {"key": "pppoe_user", "value": "beto"}])
    # Duplicate "carla": the cancelled one loses even though it is newer.
    keep = _service(conn, co, cl, plan, created="now() - interval '2 days'", conn_params={"pppoe_user": "carla"})
    lose = _service(conn, co, cl, plan, status="CANCELLED", conn_params={"pppoe_user": "carla"})
    bad = _service(conn, co, cl, plan, conn_params={"pppoe_user": "with space"})
    weird = _service(conn, co, cl, plan, conn_params=["not", "an", "object"])
    _x(conn, "UPDATE client_service SET pppoe_username = NULL WHERE company_id = :co", co=co)
    sm1 = load("versions/sm1_snmp_traffic_metrics.py", "sm1_snmp_traffic_metrics")
    _x(conn, sm1._BACKFILL)
    assert (_user(conn, a), _user(conn, b)) == ("ana", "beto")
    assert (_user(conn, keep), _user(conn, lose)) == ("carla", None)
    assert (_user(conn, bad), _user(conn, weird)) == (None, None)


def test_pppoe_username_unique_per_company(conn):
    co, cl, plan = _company(conn)
    _service(conn, co, cl, plan, user="dup")
    co2, cl2, plan2 = _company(conn)
    _service(conn, co2, cl2, plan2, user="dup")  # another tenant: fine
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _service(conn, co, cl, plan, user="dup")


def test_rows_land_in_their_day_partition(conn):
    co, cl, plan = _company(conn)
    svc = _service(conn, co, cl, plan, user="p1")
    b = datetime.combine(_today(), datetime.min.time(), GUATEMALA_TZ) + timedelta(hours=10)
    _x(conn, INSERT_5M, s=svc, co=co, b=b, da=100, dm=200, n=5)
    part = _x(conn, "SELECT tableoid::regclass::text FROM traffic_5m WHERE client_service_id = :s", s=svc).scalar()
    assert part == tp.partition_name(_today())


def test_missing_partition_fails_and_helper_creates_it(conn):
    co, cl, plan = _company(conn)
    svc = _service(conn, co, cl, plan, user="p2")
    far = _today() + timedelta(days=40)
    b = datetime.combine(far, datetime.min.time(), GUATEMALA_TZ)
    with pytest.raises(sa.exc.IntegrityError):  # no partition, no DEFAULT
        with conn.begin_nested():
            _x(conn, INSERT_5M, s=svc, co=co, b=b, da=1, dm=1, n=1)
    assert tp.ensure_traffic_5m_partitions(conn, far, 2) == [tp.partition_name(far), tp.partition_name(far + timedelta(days=1))]
    assert tp.ensure_traffic_5m_partitions(conn, far, 1) == [tp.partition_name(far)]  # idempotent
    _x(conn, INSERT_5M, s=svc, co=co, b=b, da=1, dm=1, n=1)
    assert far in tp.list_traffic_5m_partitions(conn)


def test_drop_partitions_before(conn):
    old = date(2020, 1, 1)
    tp.ensure_traffic_5m_partitions(conn, old, 2)
    dropped = tp.drop_traffic_5m_partitions_before(conn, old + timedelta(days=1))
    assert dropped == [tp.partition_name(old)]
    days = tp.list_traffic_5m_partitions(conn)
    assert old not in days and old + timedelta(days=1) in days


def test_checks(conn):
    co, cl, plan = _company(conn)
    svc = _service(conn, co, cl, plan, user="p3")
    b = datetime.combine(_today(), datetime.min.time(), GUATEMALA_TZ)
    for da, dm, n in ((5, 1, 5), (-1, 1, 5), (1, 2, 0), (1, 2, 6)):
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                _x(conn, INSERT_5M, s=svc, co=co, b=b, da=da, dm=dm, n=n)
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _x(conn, "INSERT INTO saturation_event (id, created_at, company_id, client_service_id, day, "
                     "direction, plan_mbps, threshold_pct, saturated_minutes, peak_pct) "
                     "VALUES (:i, now(), :co, :s, current_date, 'SIDE', 10, 90, 30, 95)",
               i=uuid.uuid4(), co=co, s=svc)


def test_deleting_a_service_cascades(conn):
    co, cl, plan = _company(conn)
    svc = _service(conn, co, cl, plan, user="p4")
    b = datetime.combine(_today(), datetime.min.time(), GUATEMALA_TZ)
    _x(conn, INSERT_5M, s=svc, co=co, b=b, da=1, dm=1, n=1)
    _x(conn, "INSERT INTO traffic_1d (client_service_id, company_id, day, down_avg_bps, down_max_bps, "
             "down_p95_bps, up_avg_bps, up_max_bps, up_p95_bps, samples) "
             "VALUES (:s, :co, current_date, 1, 1, 1, 1, 1, 1, 1)", s=svc, co=co)
    _x(conn, "DELETE FROM client_service WHERE id = :s", s=svc)
    for t in ("traffic_5m", "traffic_1d"):
        assert _x(conn, f"SELECT count(*) FROM {t} WHERE client_service_id = :s", s=svc).scalar() == 0


def test_downgrade_then_upgrade_is_idempotent(conn):
    sm1 = load("versions/sm1_snmp_traffic_metrics.py", "sm1_snmp_traffic_metrics")
    col = ("SELECT count(*) FROM information_schema.columns WHERE table_name = 'client_service' "
           "AND column_name = 'pppoe_username'")
    with Operations.context(MigrationContext.configure(conn)):
        sm1.downgrade()
        sm1.downgrade()
        assert _x(conn, col).scalar() == 0
        assert _x(conn, "SELECT to_regclass('traffic_5m')").scalar() is None
        sm1.upgrade()
        sm1.upgrade()
    assert _x(conn, col).scalar() == 1
    assert tp.partition_name(_today()) in [tp.partition_name(d) for d in tp.list_traffic_5m_partitions(conn)]


def test_ensure_is_a_noop_on_a_plain_table(conn):
    _x(conn, "CREATE TEMP TABLE IF NOT EXISTS sm1_probe (x int)")
    # traffic_5m here is the partitioned one; make the check see a plain one.
    _x(conn, "ALTER TABLE traffic_5m RENAME TO traffic_5m_parent_tmp")
    _x(conn, "CREATE TABLE traffic_5m (x int)")
    assert tp.ensure_traffic_5m_partitions(conn, _today(), 3) == []
