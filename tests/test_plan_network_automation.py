"""pa1 (doc 56): per-plan network_automation flag + allowed CPE categories,
the RECONNECT job kind and the client_services.cancel permission."""
import uuid

import sqlalchemy as sa
from _mi_helpers import isp_seed, load

from database_utils.models.crm import TaskJobKind
from database_utils.models.isp import DeviceCategory, ServicePlan
from database_utils.schemas.service_plan import ServicePlanCreate, ServicePlanOut, ServicePlanUpdate


def _company(db):
    from database_utils.models.auth import Company, Tier
    tier = Tier(name=f"t-{uuid.uuid4()}")
    db.add(tier)
    db.flush()
    co = Company(name=f"co-{uuid.uuid4()}", tier_id=tier.id)
    db.add(co)
    db.flush()
    return co


def test_new_plan_defaults_to_manual():
    plan = ServicePlanCreate(name="X", price=1)
    assert plan.network_automation is False
    assert plan.allowed_device_category_ids == []


def test_update_leaves_both_unset():
    patch = ServicePlanUpdate()
    assert patch.network_automation is None
    assert patch.allowed_device_category_ids is None


def test_db_default_keeps_existing_plans_automated(db):
    co = _company(db)
    plan_id = uuid.uuid4()
    db.execute(sa.text(
        "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, is_active, kind, company_id) "
        "VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'old', 'FIBER', 1, 1, 'SERVICE', :co)"
    ), {"id": plan_id.hex, "co": co.id.hex})
    assert db.get(ServicePlan, plan_id).network_automation is True


def test_allowed_categories_roundtrip(db):
    co = _company(db)
    cat = DeviceCategory(key=f"ONU-{uuid.uuid4().hex[:6]}", name="ONU")
    plan = ServicePlan(name="P", company_id=co.id, allowed_device_categories=[cat])
    db.add(plan)
    db.flush()
    db.expire_all()
    plan = db.get(ServicePlan, plan.id)
    assert ServicePlanOut.model_validate(plan).allowed_device_category_ids == [cat.id]


def test_reconnect_job_kind():
    assert TaskJobKind("RECONNECT") is TaskJobKind.RECONNECT


def test_cancel_permission_seeded():
    seed = isp_seed()
    assert "client_services.cancel" in {p["name"] for p in seed.ISP_PERMISSIONS}
    for role, spec in seed.ISP_ROLES.items():
        assert "client_services.cancel" not in spec["permissions"], role


def test_revision_permission_matches_seed():
    pa1 = load("versions/pa1_plan_network_automation.py", "pa1_plan_network_automation")
    seeded = next(p for p in isp_seed().ISP_PERMISSIONS if p["name"] == "client_services.cancel")
    assert pa1.CANCEL_PERMISSION == seeded


def test_micronode_revision_matches_seed():
    pa2 = load("versions/pa2_micronode_category.py", "pa2_micronode_category")
    seed_rows = {row[0]: row for row in isp_seed().DEVICE_CATEGORIES}
    assert pa2.MICRONODE == seed_rows["MICRONODE"]
    assert pa2.down_revision == "pa1_plan_network_automation"
    assert len(pa2.revision) <= 32
