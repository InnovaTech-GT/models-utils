"""sm1: traffic tables,
traffic settings and the partition helpers off PostgreSQL."""
import uuid
from datetime import date, datetime, timedelta

import pytest
from _mi_helpers import load
from conftest import CO_A, CO_B
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    TRAFFIC_SETTINGS_DEFAULTS,
    ClientService,
    SaturationEvent,
    Traffic5m,
)
from database_utils.schemas.company import CompanyUpdate, TrafficSettings
from database_utils.utils import traffic_partitions as tp
from database_utils.utils.timezone_utils import GUATEMALA_TZ


def _service(db, company=CO_A, status=None):
    cs = ClientService(id=uuid.uuid4(), company_id=company, client_id=uuid.uuid4(),
                       service_plan_id=uuid.uuid4())
    if status is not None:
        cs.status = status
    db.add(cs)
    db.flush()
    return cs


def test_traffic_row_and_saturation_event(db):
    cs = _service(db)
    start = datetime(2026, 11, 8, 10, 5, tzinfo=GUATEMALA_TZ)
    db.add(Traffic5m(client_service_id=cs.id, company_id=CO_A, bucket_start=start,
                     down_avg_bps=8_000_000, down_max_bps=9_500_000,
                     up_avg_bps=1_000_000, up_max_bps=2_000_000, samples=5))
    db.add(SaturationEvent(company_id=CO_A, client_service_id=cs.id, day=date(2026, 11, 8),
                           direction="DOWN", plan_mbps=10, threshold_pct=90,
                           saturated_minutes=45, peak_pct=96))
    db.commit()
    row = db.get(Traffic5m, (cs.id, start))
    assert row.down_dropped == 0 and row.up_dropped == 0
    db.add(SaturationEvent(company_id=CO_A, client_service_id=cs.id, day=date(2026, 11, 8),
                           direction="DOWN", plan_mbps=10, threshold_pct=90,
                           saturated_minutes=50, peak_pct=97))
    with pytest.raises(IntegrityError):  # one event per service, day and direction
        db.commit()


def test_traffic_settings_defaults_match_the_model_constant():
    assert TrafficSettings().model_dump() == TRAFFIC_SETTINGS_DEFAULTS


def test_traffic_settings_validation():
    assert CompanyUpdate(traffic_settings={"saturation_pct": 85}).traffic_settings.saturation_pct == 85
    with pytest.raises(ValidationError):
        TrafficSettings(saturation_pct=120)
    with pytest.raises(ValidationError):
        TrafficSettings(retention_5m_days=30, retention_1h_days=20)
    with pytest.raises(ValidationError):
        TrafficSettings(unknown=1)


def test_partition_helpers_are_noops_off_postgres(db):
    bind = db.connection()
    assert tp.ensure_traffic_5m_partitions(bind, date(2026, 11, 8), 3) == []
    assert tp.list_traffic_5m_partitions(bind) == []
    assert tp.drop_traffic_5m_partitions_before(bind, date(2026, 11, 8)) == []


def test_guatemala_day():
    from datetime import timezone
    assert tp.guatemala_day(date(2026, 11, 8)) == date(2026, 11, 8)
    # 02:00 UTC is 20:00 of the previous day in Guatemala, 23:00 UTC is 17:00 the same day.
    assert tp.guatemala_day(datetime(2026, 11, 9, 2, tzinfo=timezone.utc)) == date(2026, 11, 8)
    assert tp.guatemala_day(datetime(2026, 11, 8, 23)) == date(2026, 11, 8)  # naive = UTC
    assert tp.guatemala_day(datetime(2026, 11, 8, 23, 30, tzinfo=GUATEMALA_TZ)) == date(2026, 11, 8)


def test_partition_name():
    assert tp.partition_name(date(2026, 11, 8)) == "traffic_5m_20261108"
    assert tp._bound(date(2026, 11, 8)) == "2026-11-08T00:00:00-06:00"
    assert tp._bound(date(2026, 11, 8) + timedelta(days=1)).startswith("2026-11-09")


def test_revision_chain():
    m = load("versions/sm1_snmp_traffic_metrics.py", "sm1_snmp_traffic_metrics")
    assert (m.revision, m.down_revision) == ("sm1_snmp_traffic_metrics", "cdn1_credit_detail_null")
