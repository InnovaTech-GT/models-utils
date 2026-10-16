"""pz1 (6.11.0): company.provisioning_zone and the worker's zone filter —
company_in_zone (NULL = the default zone) and repair_stranded_runs(where=)."""
import uuid
from datetime import timedelta

import sqlalchemy as sa

from database_utils.models.auth import Company, Tier
from database_utils.models.isp import PURPOSE_ACTIVATION, ProvisioningJob, ProvisioningRun
from database_utils.models.isp import ProvisioningJobStatus as S
from database_utils.utils.provisioning_jobs import company_in_zone
from database_utils.utils.provisioning_runs import create_run, repair_stranded_runs
from database_utils.utils.timezone_utils import now_gt


def _companies(db, **zones):
    tier = Tier(id=uuid.uuid4(), name=f"t-{uuid.uuid4()}")
    db.add(tier)
    ids = {}
    for name, zone in zones.items():
        ids[name] = uuid.uuid4()
        db.add(Company(id=ids[name], name=f"{name}-{ids[name]}", tier_id=tier.id,
                       provisioning_zone=zone))
    db.flush()
    return ids


def _in(db, zone):
    return set(db.execute(sa.select(Company.id).where(company_in_zone(Company.id, zone))).scalars())


def test_company_in_zone_null_is_the_default_zone(db):
    ids = _companies(db, plain=None, csr="do-dev-csr", other="do-dev-x")
    assert _in(db, None) == {ids["plain"]}
    assert _in(db, "") == {ids["plain"]}
    assert _in(db, "do-dev-csr") == {ids["csr"]}
    assert _in(db, "nope") == set()


def test_repair_stranded_runs_where_limits_to_the_zone(db, plant):
    tier = Tier(id=uuid.uuid4(), name=f"t-{uuid.uuid4()}")
    db.add(tier)
    db.add(Company(id=plant.company_id, name=f"plant-{plant.company_id}", tier_id=tier.id,
                   provisioning_zone="do-dev-csr"))
    db.flush()
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    child = db.execute(sa.select(ProvisioningJob).where(ProvisioningJob.run_id == run.id)).scalar_one()
    child.status = S.SUCCEEDED
    run.updated_at = now_gt() - timedelta(minutes=5)
    db.flush()

    assert repair_stranded_runs(db, where=company_in_zone(ProvisioningRun.company_id, None)) == 0
    assert run.status == S.QUEUED
    assert repair_stranded_runs(db, where=company_in_zone(ProvisioningRun.company_id, "do-dev-csr")) == 1
