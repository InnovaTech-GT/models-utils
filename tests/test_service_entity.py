"""sv1 (doc 58): Service entity — ORM, derived plan properties, schemas."""
import uuid

import pytest
from _mi_helpers import load
from sqlalchemy.exc import IntegrityError

from database_utils.models.auth import Company, Tier
from database_utils.models.isp import DeviceCategory, Service, ServicePlan, ServicePlanType
from database_utils.schemas.service_plan import (
    ServiceCreate, ServiceOut, ServicePlanCreate, ServicePlanOut, ServicePlanUpdate, ServiceUpdate,
)


def _company(db):
    tier = Tier(name=f"t-{uuid.uuid4()}")
    db.add(tier)
    db.flush()
    co = Company(name=f"co-{uuid.uuid4()}", tier_id=tier.id)
    db.add(co)
    db.flush()
    return co


def test_service_create_defaults():
    s = ServiceCreate(name="Internet GPON")
    assert (s.service_type, s.network_automation, s.allowed_device_category_ids) == (ServicePlanType.FIBER, False, [])


def test_service_update_all_optional():
    assert ServiceUpdate().model_dump(exclude_unset=True) == {}


def test_plan_schemas_take_service_id_speeds_optional():
    c = ServicePlanCreate(name="P", service_id=uuid.uuid4())
    assert c.upload_mbps is None and c.download_mbps is None
    assert ServicePlanUpdate(service_id=uuid.uuid4()).service_id


def test_plan_properties_follow_service_and_out_schemas(db):
    co = _company(db)
    cat = DeviceCategory(key=f"ONU-{uuid.uuid4().hex[:6]}", name="ONU")
    svc = Service(company_id=co.id, name="Cable", service_type=ServicePlanType.CABLE,
                  allowed_device_categories=[cat])
    p1 = ServicePlan(name="A", company_id=co.id, service=svc, is_active=False)
    p2 = ServicePlan(name="B", company_id=co.id, service=svc, is_active=True)
    db.add_all([svc, p1, p2])
    db.flush()
    db.expire_all()
    p1 = db.get(ServicePlan, p1.id)
    assert p1.network_automation is False  # service default: manual
    assert p1.allowed_device_category_ids == [cat.id]
    out = ServicePlanOut.model_validate(p1)
    assert out.service_id == svc.id and out.allowed_device_category_ids == [cat.id] and out.network_automation is False
    s_out = ServiceOut.model_validate(db.get(Service, svc.id))
    assert s_out.is_active is True and s_out.plan_count == 2 and len(s_out.plans) == 2
    assert s_out.allowed_device_category_ids == [cat.id]
    svc.network_automation = True
    assert p1.network_automation is True


def test_service_name_unique_per_company(db):
    co = _company(db)
    db.add(Service(company_id=co.id, name="X"))
    db.flush()
    db.add(Service(company_id=co.id, name="X"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_revision_chain_and_length():
    m = load("versions/sv1_service_entity.py", "sv1_service_entity")
    assert m.down_revision == "pa2_micronode_category" and len(m.revision) <= 32
