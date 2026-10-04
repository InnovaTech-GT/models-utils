"""ld1_legacy_drop guardrails: chain position, env.py seed sentinel, removed code."""
import importlib.util
import os
import re

import pytest

from database_utils import models
from database_utils.models.crm import Order, OrderItem, Task, TaskLinkedObjectType
from database_utils.models.isp import ClientService, ServicePlan

_HERE = os.path.dirname(__file__)
_ALEMBIC = os.path.join(_HERE, "..", "alembic")


def _ld1():
    path = os.path.join(_ALEMBIC, "versions", "ld1_legacy_drop.py")
    spec = importlib.util.spec_from_file_location("ld1_legacy_drop", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_position():
    ld1 = _ld1()
    assert ld1.revision == "ld1_legacy_drop"
    assert ld1.down_revision == "rr1_four_builtin_roles"
    assert len(ld1.revision) <= 32


def test_downgrade_refuses():
    with pytest.raises(NotImplementedError):
        _ld1().downgrade()


def test_single_head():
    revs, downs = set(), set()
    for name in os.listdir(os.path.join(_ALEMBIC, "versions")):
        if not name.endswith(".py"):
            continue
        body = open(os.path.join(_ALEMBIC, "versions", name)).read()
        r = re.search(r'^revision(?:: str)?\s*=\s*["\'](.+?)["\']', body, re.M)
        d = re.search(r'^down_revision[^=]*=\s*(.+)$', body, re.M)
        revs.add(r.group(1))
        downs.update(re.findall(r'["\'](.+?)["\']', d.group(1)))
    assert revs - downs == {"ld1_legacy_drop"}


def test_env_seed_sentinel_is_not_a_dropped_table():
    body = open(os.path.join(_ALEMBIC, "env.py")).read()
    assert "table_name = 'client_service'" in body
    assert "table_name = 'workflow_template'" not in body


def test_dropped_models_are_gone():
    for name in ("Product", "RecurringOrder", "RecurringOrderItem", "TaskState",
                 "TaskStateColor", "WorkflowTemplate"):
        assert not hasattr(models, name), name
    from database_utils.database import Base
    tables = set(Base.metadata.tables)
    assert tables.isdisjoint(_ld1().DROP_TABLES)
    for model, column in ((Order, "recurring_order_id"), (OrderItem, "product_id"),
                          (ServicePlan, "product_id"), (ClientService, "recurring_order_id"),
                          (Task, "task_state_id")):
        assert column not in model.__table__.columns, (model, column)
    assert not hasattr(TaskLinkedObjectType, "RECURRING_ORDER")


def test_kept_billing_schemas_still_exported():
    from database_utils import schemas
    for name in ("DueBillingItemOut", "OrderGenerationResponse", "MissingPeriod",
                 "GeneratedOrdersWithGaps", "RegeneratePeriodRequest", "RegeneratePeriodResponse"):
        assert hasattr(schemas, name), name
    from database_utils.models.crm import RecurrenceEnum, RecurringOrderStatus  # noqa: F401


def test_seeds_no_longer_know_the_legacy_permissions():
    for fname in ("rbac_seed.py", "isp_seed.py"):
        body = open(os.path.join(_ALEMBIC, "seeds", fname)).read()
        assert not re.search(r'"(products|recurring_orders|task_states|workflow_templates)\.', body), fname
