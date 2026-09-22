"""Shared fixtures for the models-utils suite.

The `plant` fixture builds a real in-memory SQLite network graph rather than a
set of fakes. Resolution now runs recursive CTEs and two binding lookups, so a
hand-rolled fake DB would assert the shape of the mock instead of the behaviour
of the query.

The seeded plant is one company's chain, root-first:

    CORE-1 (router) -> OLT-1 (olt) -> SPL-1 (splitter) -> SPL-2 (splitter)
                                                       -> ONT-1 (onu)

Both splitters are passive. ACTIVATION and SUSPENSION playbooks are bound to
the router, olt and onu device TYPES.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database_utils.database import Base
from database_utils.models.isp import (
    ClientService,
    DeviceCategory,
    DeviceType,
    DeviceTypePlaybook,
    InventoryItem,
    InventoryItemPlaybook,
    Playbook,
    PURPOSE_ACTIVATION,
    ServicePlan,
)


CO_A = uuid.uuid4()
CO_B = uuid.uuid4()

# A definition that reads a device variable, so amendment-4 fatality applies.
_DEF = {"steps": [{"name": "s", "driver": "simulator",
                   "template": "sn {{ cpe.serial }}"}]}
# A definition that reads no device variable at all.
_DEF_DEVICE_FREE = {"steps": [{"name": "s", "driver": "simulator",
                               "template": "noop"}]}


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class Plant:
    """The seeded graph, exposed as attributes the tests can poke at."""

    def __init__(self, db, company_id=CO_A):
        self.db = db
        self.company_id = company_id
        self.categories = {}
        self.types = {}

        for key, tier, passive in (
            ("ROUTER", "CORE", False),
            ("OLT", "CORE", False),
            ("SPLITTER", None, True),
            ("ONU", "EDGE", False),
        ):
            cat = DeviceCategory(id=uuid.uuid4(), key=key, name=key.title(),
                                 tier=tier, is_passive=passive)
            db.add(cat)
            self.categories[key] = cat
        db.flush()

        for key in self.categories:
            dt = DeviceType(id=uuid.uuid4(), company_id=company_id,
                            name=f"{key} model", category_id=self.categories[key].id)
            db.add(dt)
            self.types[key] = dt
        db.flush()

        self.core = self._item("CORE-1", "ROUTER", None)
        self.olt = self._item("OLT-1", "OLT", self.core)
        self.spl1 = self._item("SPL-1", "SPLITTER", self.olt)
        self.spl2 = self._item("SPL-2", "SPLITTER", self.spl1)
        self.cpe = self._item("ONT-1", "ONU", self.spl2)

        self.plan = ServicePlan(id=uuid.uuid4(), company_id=company_id,
                                name="Fibra 100", download_mbps=100, upload_mbps=20)
        db.add(self.plan)
        db.flush()

        self.service = ClientService(
            id=uuid.uuid4(), company_id=company_id, client_id=uuid.uuid4(),
            service_plan_id=self.plan.id, cpe_item_id=self.cpe.id,
        )
        db.add(self.service)
        db.flush()
        self.cpe.client_service_id = self.service.id
        db.flush()

        self.playbooks = {}
        for key in ("ROUTER", "OLT", "ONU"):
            for purpose in (PURPOSE_ACTIVATION, "SUSPENSION"):
                self.bind_type(self.types[key], purpose,
                               self._playbook(f"{key.lower()}-{purpose.lower()}"))

    def _item(self, serial, category_key, parent):
        item = InventoryItem(
            id=uuid.uuid4(), company_id=self.company_id,
            device_type_id=self.types[category_key].id, serial_number=serial,
            mac_address=f"AA:BB:{serial}", mgmt_host=f"10.0.0.{len(serial)}",
            network_attached=True,
            parent_id=parent.id if parent is not None else None,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def _playbook(self, name, definition=None):
        pb = Playbook(id=uuid.uuid4(), company_id=self.company_id, name=name,
                      is_active=True, definition=definition or _DEF)
        self.db.add(pb)
        self.db.flush()
        self.playbooks[name] = pb
        return pb

    def bind_type(self, device_type, purpose, playbook):
        self.db.add(DeviceTypePlaybook(
            id=uuid.uuid4(), company_id=self.company_id,
            device_type_id=device_type.id, purpose=purpose, playbook_id=playbook.id,
        ))
        self.db.flush()

    def bind_node(self, item, purpose, playbook):
        self.db.add(InventoryItemPlaybook(
            id=uuid.uuid4(), company_id=self.company_id,
            inventory_item_id=item.id, purpose=purpose, playbook_id=playbook.id,
        ))
        self.db.flush()

    def unbind_type(self, device_type, purpose):
        self.db.execute(sa.delete(DeviceTypePlaybook.__table__).where(
            DeviceTypePlaybook.__table__.c.device_type_id == device_type.id,
            DeviceTypePlaybook.__table__.c.purpose == purpose,
        ))
        self.db.flush()


@pytest.fixture()
def plant(db):
    return Plant(db)


