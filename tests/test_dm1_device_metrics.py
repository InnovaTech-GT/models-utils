"""dm1 (doc 75): device metric schemas and tables (SQLite)."""
from datetime import datetime, timedelta, timezone

import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models.device_metric import DeviceMetric, DeviceMetric5m
from database_utils.schemas.device_metric import (DeviceMetricCreate, DeviceMetricOut,
                                                  DeviceMetricUpdate, normalize_oid)

GT = timezone(timedelta(hours=-6))


def test_oid_rules():
    assert normalize_oid(" .1.3.6.1.2.1.25.3.3.1.2 ") == "1.3.6.1.2.1.25.3.3.1.2"
    assert normalize_oid("") is None
    for bad in ("sysUpTime.0", "1.3.6", "2.3.6.1.4", "1.3.6.1..2", "1.3.6.1.x", "1." + "1." * 120 + "1"):
        with pytest.raises(ValueError, match="INVALID_OID"):
            normalize_oid(bad)


def test_create_needs_exactly_one_scope(plant):
    base = {"name": "CPU", "oid": "1.3.6.1.2.1.25.3.3.1.2"}
    with pytest.raises(ValidationError, match="SCOPE_REQUIRED"):
        DeviceMetricCreate(**base)
    with pytest.raises(ValidationError, match="SCOPE_REQUIRED"):
        DeviceMetricCreate(**base, device_type_id=plant.core.device_type_id,
                           inventory_item_id=plant.core.id)
    m = DeviceMetricCreate(**base, device_type_id=plant.core.device_type_id,
                           instances=[" a ", "a", "b"])
    assert m.instances == ["a", "b"] and m.kind == "GAUGE" and m.scale == 1.0


def test_limits_and_partial_update():
    with pytest.raises(ValidationError):
        DeviceMetricUpdate(instances=[str(i) for i in range(65)])
    with pytest.raises(ValidationError):
        DeviceMetricUpdate(scale=0)
    with pytest.raises(ValidationError):
        DeviceMetricUpdate(kind="DERIVE")
    with pytest.raises(ValidationError):
        DeviceMetricUpdate(device_type_id="00000000-0000-0000-0000-000000000000")  # scope is fixed
    assert DeviceMetricUpdate(enabled=False).model_dump(exclude_unset=True) == {"enabled": False}


def test_tables_round_trip(db, plant):
    m = DeviceMetric(company_id=plant.company_id, name="Temp", oid="1.3.6.1.4.1.14988.1.1.3.100.1.3",
                     label_oid="1.3.6.1.4.1.14988.1.1.3.100.1.2", instances=["cpu-temperature"],
                     device_type_id=plant.core.device_type_id, unit="°C")
    db.add(m)
    db.flush()
    db.add(DeviceMetric5m(metric_id=m.id, inventory_item_id=plant.core.id, instance="cpu-temperature",
                          bucket_start=datetime(2026, 11, 20, 10, 5, tzinfo=GT),
                          company_id=plant.company_id, value_avg=39.2, value_min=39, value_max=40,
                          samples=5))
    db.flush()
    out = DeviceMetricOut.model_validate(m)
    assert out.kind == "GAUGE" and out.enabled and out.instances == ["cpu-temperature"]
    assert db.query(DeviceMetric5m).one().value_avg == 39.2


def test_dm1_revision_chain():
    m = load("versions/dm1_device_metrics.py", "dm1_device_metrics")
    assert (m.revision, m.down_revision) == ("dm1_device_metrics", "tm2_drop_pppoe_username")
