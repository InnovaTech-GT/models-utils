"""provisioning_runs guarantees that only Postgres can prove
(provisioning-concurrency fix): the savepoint + partial unique index dedupe in
create_or_get_run, the run-row lock in advance_run, and SKIP LOCKED in
repair_stranded_runs.

Run against a database already at `alembic upgrade head`:

    PG_TEST_URL=postgresql://erp:erp@localhost:5432/<throwaway> pytest -m pg tests/pg

Skipped when PG_TEST_URL is unset. Unlike test_port_topology_pg these tests
need several connections that really COMMIT, so each test seeds its own
company and deletes it afterwards.
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database_utils.models import ProvisioningRun
from database_utils.models.isp import (
    ClientService,
    ProvisioningJob,
    ProvisioningJobStatus,
    PURPOSE_ACTIVATION,
)
from database_utils.utils import provisioning_runs
from database_utils.utils.provisioning_resolution import ResolvedProvisioning
from database_utils.utils.provisioning_runs import (
    advance_run,
    create_or_get_run,
    create_run,
    repair_stranded_runs,
)
from database_utils.utils.timezone_utils import now_gt

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]


@pytest.fixture()
def engine():
    eng = sa.create_engine(os.environ["PG_TEST_URL"], pool_size=12)
    yield eng
    eng.dispose()


@pytest.fixture()
def seed(engine):
    """One company, one service, two devices with a playbook each."""
    co, client, plan, svc = (uuid.uuid4() for _ in range(4))
    dtype, pb = uuid.uuid4(), uuid.uuid4()
    cpe, olt = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        x = lambda sql, **p: c.execute(sa.text(sql), p)  # noqa: E731
        x("INSERT INTO company (id, created_at, name, tier_id) "
          "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"pc-test-{co}")
        x("INSERT INTO client (id, created_at, name, company_id) "
          "VALUES (:id, now(), 'pc client', :co)", id=client, co=co)
        x("INSERT INTO service_plan (id, created_at, updated_at, name, price, is_active, "
          "plan_type, company_id) VALUES (:id, now(), now(), 'pc plan', 1, true, "
          "'FIBER', :co)", id=plan, co=co)
        x("INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
          "service_plan_id) VALUES (:id, now(), now(), :co, :cl, :p)",
          id=svc, co=co, cl=client, p=plan)
        x("INSERT INTO device_type (id, created_at, updated_at, name, company_id, category_id) "
          "SELECT :id, now(), now(), 'pc type', :co, id FROM device_category LIMIT 1",
          id=dtype, co=co)
        for item in (cpe, olt):
            x("INSERT INTO inventory_item (id, created_at, updated_at, company_id, "
              "device_type_id, network_attached) VALUES (:id, now(), now(), :co, :t, true)",
              id=item, co=co, t=dtype)
        x("INSERT INTO playbook (id, created_at, updated_at, company_id, name, definition, "
          "is_active, version) VALUES (:id, now(), now(), :co, 'pc pb', "
          "'{\"steps\": []}', true, 1)", id=pb, co=co)
    resolution = ResolvedProvisioning(steps=[
        SimpleNamespace(item_id=item, playbook_id=pb, playbook_version=1, category_key=k)
        for item, k in ((cpe, "onu"), (olt, "olt"))
    ])
    yield SimpleNamespace(co=co, svc=svc, resolution=resolution)
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM provisioning_run WHERE company_id = :co"), {"co": co})
        c.execute(sa.text("DELETE FROM provisioning_job WHERE company_id = :co"), {"co": co})
        c.execute(sa.text("DELETE FROM company WHERE id = :co"), {"co": co})


def _sessions(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _count(engine, model, co):
    with Session(engine) as s:
        return s.execute(sa.select(sa.func.count()).select_from(model)
                         .where(model.company_id == co)).scalar()


def test_concurrent_create_or_get_run(engine, seed, monkeypatch):
    """8 producers race on one key. Every thread's pre-check is forced to miss,
    so all 8 reach the INSERT: exactly one run exists, no IntegrityError
    escapes, and each loser's session still commits other work."""
    real = provisioning_runs.find_in_flight_run
    local = threading.local()

    def _blind_first(*a, **kw):
        if not getattr(local, "seen", False):
            local.seen = True
            return None
        return real(*a, **kw)

    monkeypatch.setattr(provisioning_runs, "find_in_flight_run", _blind_first)
    Sess = _sessions(engine)
    barrier = threading.Barrier(8)

    def producer(_):
        local.seen = False
        with Sess() as db:
            svc = db.get(ClientService, seed.svc)
            barrier.wait()
            run, created = create_or_get_run(db, svc, PURPOSE_ACTIVATION,
                                             resolution=seed.resolution)
            # The caller's own transaction survived the savepoint rollback.
            db.execute(sa.text("SELECT 1"))
            db.commit()
            return run.id, created

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(producer, range(8)))

    assert sum(created for _, created in results) == 1
    assert len({rid for rid, _ in results}) == 1
    assert _count(engine, ProvisioningRun, seed.co) == 1
    assert _count(engine, ProvisioningJob, seed.co) == 1


def test_concurrent_advance_queues_the_next_child_once(engine, seed):
    """Two settles advance the same SUCCEEDED child at once (a reaped duplicate
    or cancel racing a settle). The run-row lock serializes them and the
    second sees the child the first queued: one child 1, no IntegrityError."""
    Sess = _sessions(engine)
    with Sess() as db:
        run = create_run(db, db.get(ClientService, seed.svc), PURPOSE_ACTIVATION,
                         resolution=seed.resolution)
        child = db.execute(sa.select(ProvisioningJob)
                           .where(ProvisioningJob.run_id == run.id)).scalar_one()
        child.status = ProvisioningJobStatus.SUCCEEDED
        db.commit()
        child_id = child.id

    a = Sess()
    nxt = advance_run(a, a.get(ProvisioningJob, child_id))   # holds the run row
    assert nxt is not None and nxt.run_position == 1

    def second():
        with Sess() as b:
            got = advance_run(b, b.get(ProvisioningJob, child_id))
            b.commit()
            return got

    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(second)
        with pytest.raises(TimeoutError):
            fut.result(timeout=0.5)        # blocked on the run row lock
        a.commit()
        a.close()
        assert fut.result(timeout=10) is None
    with Session(engine) as s:
        positions = s.execute(sa.select(ProvisioningJob.run_position)
                              .where(ProvisioningJob.company_id == seed.co)
                              .order_by(ProvisioningJob.run_position)).scalars().all()
    assert positions == [0, 1]


def test_repair_skips_a_run_another_transaction_holds(engine, seed):
    Sess = _sessions(engine)
    with Sess() as db:
        run = create_run(db, db.get(ClientService, seed.svc), PURPOSE_ACTIVATION,
                         resolution=seed.resolution)
        db.execute(sa.update(ProvisioningJob).where(ProvisioningJob.run_id == run.id)
                   .values(status=ProvisioningJobStatus.FAILED))
        run.updated_at = now_gt() - timedelta(minutes=5)
        db.commit()
        run_id = run.id

    holder = engine.connect()
    tx = holder.begin()
    holder.execute(sa.text("SELECT 1 FROM provisioning_run WHERE id = :id FOR UPDATE"),
                   {"id": run_id})
    try:
        with Sess() as db:
            assert repair_stranded_runs(db) == 0, "a locked run is skipped, not waited on"
            db.commit()
    finally:
        tx.rollback()
        holder.close()

    with Sess() as db:
        assert repair_stranded_runs(db) >= 1
        db.commit()
        assert db.get(ProvisioningRun, run_id).status == ProvisioningJobStatus.FAILED
