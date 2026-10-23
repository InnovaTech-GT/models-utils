"""ca1: collection areas — model, schemas, revision parity and permissions."""
import uuid

import pytest
from _mi_helpers import load
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import CollectionArea, User
from database_utils.schemas import (
    CollectionAreaCreate,
    CollectionAreaOut,
    CollectionAreaUpdate,
    CollectorHomeUpdate,
    normalize_polygon,
)

SQUARE = [[14.60, -90.55], [14.60, -90.50], [14.65, -90.50], [14.65, -90.55]]


def _ca1():
    return load("versions/ca1_collection_areas.py", "ca1_test")


def _area(company_id, name="Área 1", collector_id=None):
    return CollectionArea(company_id=company_id, name=name, color="#2563EB",
                          polygon=SQUARE, collector_id=collector_id)


def test_chain_position():
    assert _ca1().down_revision == "pz1_provisioning_zone"
    assert len(_ca1().revision) <= 32


def test_permissions_match_seed():
    rbac = load("seeds/rbac_seed.py", "rbac_ca1_test")
    names = {p["name"] for p in _ca1().PERMISSIONS}
    assert names == {"collection_areas.read", "collection_areas.update"}
    seeded = [p for p in rbac.PERMISSIONS_DATA if p["name"] in names]
    assert sorted(seeded, key=lambda p: p["name"]) == sorted(_ca1().PERMISSIONS, key=lambda p: p["name"])


def test_model_columns():
    cols = CollectionArea.__table__.c
    assert {"id", "created_at", "updated_at", "name", "color", "polygon", "company_id", "collector_id"} <= set(cols.keys())
    assert cols.collector_id.nullable and not cols.polygon.nullable
    fk = next(iter(cols.collector_id.foreign_keys))
    assert fk.ondelete == "SET NULL"
    assert {"home_latitude", "home_longitude"} <= set(User.__table__.c.keys())


def test_area_round_trip(db):
    company_id = uuid.uuid4()
    db.add(_area(company_id))
    db.commit()
    area = db.query(CollectionArea).one()
    out = CollectionAreaOut.model_validate(area)
    assert out.polygon == SQUARE and out.collector_id is None and out.updated_at


def test_name_unique_per_company(db):
    company_id = uuid.uuid4()
    db.add_all([_area(company_id), _area(company_id)])
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add_all([_area(company_id), _area(uuid.uuid4())])
    db.commit()


def test_one_area_per_collector(db):
    company_id, collector = uuid.uuid4(), uuid.uuid4()
    db.add_all([_area(company_id, "A", collector), _area(company_id, "B", collector)])
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # Several areas without a collector are fine.
    db.add_all([_area(company_id, "A"), _area(company_id, "B")])
    db.commit()


def test_normalize_polygon_drops_closing_point_and_validates():
    assert normalize_polygon(SQUARE + [SQUARE[0]]) == SQUARE
    for bad in ([[14.6, -90.5], [14.7, -90.5]],          # two points
                [[14.6, -90.5], [14.6, -90.5], [14.6, -90.5], [14.6, -90.5]],  # one distinct point
                [[95, -90.5], [14.7, -90.5], [14.7, -90.4]],  # latitude out of range
                [[14.6, -90.5, 1], [14.7, -90.5], [14.7, -90.4]]):  # not a pair
        with pytest.raises(ValueError):
            normalize_polygon(bad)


def test_create_schema():
    area = CollectionAreaCreate(name="  Mixco norte ", color="#2563eb", polygon=SQUARE)
    assert area.name == "Mixco norte" and area.color == "#2563EB"
    for bad in ({"name": " ", "color": "#2563EB", "polygon": SQUARE},
                {"name": "A", "color": "blue", "polygon": SQUARE},
                {"name": "A", "color": "#2563EB", "polygon": SQUARE[:2]}):
        with pytest.raises(ValidationError):
            CollectionAreaCreate(**bad)


def test_update_schema_is_partial_and_can_clear_collector():
    update = CollectionAreaUpdate(collector_id=None)
    assert update.model_dump(exclude_unset=True) == {"collector_id": None}
    assert CollectionAreaUpdate(polygon=SQUARE + [SQUARE[0]]).polygon == SQUARE
    with pytest.raises(ValidationError):
        CollectionAreaUpdate(color="#12345")


def test_home_both_or_neither():
    assert CollectorHomeUpdate(home_latitude=14.6, home_longitude=-90.5).home_latitude == 14.6
    assert CollectorHomeUpdate().home_latitude is None
    with pytest.raises(ValidationError):
        CollectorHomeUpdate(home_latitude=14.6)
    with pytest.raises(ValidationError):
        CollectorHomeUpdate(home_latitude=14.6, home_longitude=-190)
