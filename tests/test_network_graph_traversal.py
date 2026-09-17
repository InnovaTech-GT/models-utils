"""Traversal of the company network graph (doc 35 §3).

The cross-tenant test deliberately writes an illegal edge with raw SQL, past the
service layer and past the trigger, to prove the traversal itself refuses to
follow it. Belt and braces: the guarantee is the trigger, but a query that only
filtered its anchor would still leak if the guarantee ever failed.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database_utils.database import Base
from database_utils.models.isp import InventoryItem
from database_utils.utils.network_graph import (
    MAX_PATH_DEPTH,
    GraphError,
    child_count,
    descendants,
    resolve_path,
    would_create_cycle,
)

CO_A = uuid.uuid4()
CO_B = uuid.uuid4()


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _node(db, company_id, serial, parent=None):
    item = InventoryItem(
        id=uuid.uuid4(),
        company_id=company_id,
        device_type_id=uuid.uuid4(),
        serial_number=serial,
        network_attached=True,
        parent_id=parent.id if parent is not None else None,
    )
    db.add(item)
    db.flush()
    return item


def _chain(db, company_id, serials):
    """Create an attached root->leaf chain; return items root-first."""
    parent = None
    items = []
    for serial in serials:
        parent = _node(db, company_id, serial, parent)
        items.append(parent)
    return items


def test_resolve_path_returns_leaf_to_root(db):
    core, olt, split, cpe = _chain(db, CO_A, ["core", "olt", "split", "cpe"])
    path = resolve_path(db, cpe.id, CO_A)
    assert [i.serial_number for i in path] == ["cpe", "split", "olt", "core"]


def test_resolve_path_of_a_root_is_just_itself(db):
    (core,) = _chain(db, CO_A, ["core"])
    assert [i.id for i in resolve_path(db, core.id, CO_A)] == [core.id]


def test_resolve_path_never_crosses_tenants(db):
    a_core, a_olt = _chain(db, CO_A, ["a-core", "a-olt"])
    (b_cpe,) = _chain(db, CO_B, ["b-cpe"])
    # Forcibly write a cross-tenant edge, bypassing the service layer AND the
    # trigger. The traversal must still refuse to follow it.
    db.execute(
        sa.update(InventoryItem.__table__)
        .where(InventoryItem.__table__.c.id == b_cpe.id)
        .values(parent_id=a_olt.id)
    )
    db.flush()
    path = resolve_path(db, b_cpe.id, CO_B)
    assert [i.serial_number for i in path] == ["b-cpe"]


def test_resolve_path_of_an_unknown_item_is_empty(db):
    assert resolve_path(db, uuid.uuid4(), CO_A) == []


def test_resolve_path_of_a_foreign_item_is_empty(db):
    (b_cpe,) = _chain(db, CO_B, ["b-cpe"])
    assert resolve_path(db, b_cpe.id, CO_A) == []


def test_descendants_returns_everything_behind_a_node(db):
    core, olt = _chain(db, CO_A, ["core", "olt"])
    _node(db, CO_A, "cpe1", olt)
    _node(db, CO_A, "cpe2", olt)
    got = {i.serial_number for i in descendants(db, olt.id, CO_A)}
    assert got == {"cpe1", "cpe2"}


def test_descendants_excludes_the_node_itself(db):
    core, olt = _chain(db, CO_A, ["core", "olt"])
    assert core.id not in {i.id for i in descendants(db, olt.id, CO_A)}
    assert olt.id not in {i.id for i in descendants(db, olt.id, CO_A)}


def test_descendants_is_transitive(db):
    core, olt, split = _chain(db, CO_A, ["core", "olt", "split"])
    _node(db, CO_A, "cpe", split)
    got = {i.serial_number for i in descendants(db, core.id, CO_A)}
    assert got == {"olt", "split", "cpe"}


def test_would_create_cycle_direct(db):
    core, olt = _chain(db, CO_A, ["core", "olt"])
    assert would_create_cycle(db, core.id, olt.id, CO_A) is True


def test_would_create_cycle_indirect_three_deep(db):
    a, b, c = _chain(db, CO_A, ["a", "b", "c"])
    assert would_create_cycle(db, a.id, c.id, CO_A) is True


def test_would_create_cycle_self(db):
    (a,) = _chain(db, CO_A, ["a"])
    assert would_create_cycle(db, a.id, a.id, CO_A) is True


def test_a_legal_reparent_is_not_a_cycle(db):
    core, olt = _chain(db, CO_A, ["core", "olt"])
    other = _node(db, CO_A, "olt2", core)
    assert would_create_cycle(db, olt.id, other.id, CO_A) is False


def test_resolve_path_raises_past_the_depth_cap(db):
    items = _chain(db, CO_A, [f"n{i}" for i in range(MAX_PATH_DEPTH + 2)])
    with pytest.raises(GraphError) as exc:
        resolve_path(db, items[-1].id, CO_A)
    assert exc.value.code == "PATH_TOO_DEEP"


def test_a_path_exactly_at_the_cap_is_fine(db):
    items = _chain(db, CO_A, [f"n{i}" for i in range(MAX_PATH_DEPTH)])
    assert len(resolve_path(db, items[-1].id, CO_A)) == MAX_PATH_DEPTH


def test_child_count_is_immediate_children_only(db):
    core, olt, split = _chain(db, CO_A, ["core", "olt", "split"])
    _node(db, CO_A, "cpe", split)
    assert child_count(db, olt.id, CO_A) == 1
    assert child_count(db, split.id, CO_A) == 1
    assert child_count(db, core.id, CO_A) == 1


def test_child_count_is_company_scoped(db):
    (a_core,) = _chain(db, CO_A, ["a-core"])
    assert child_count(db, a_core.id, CO_B) == 0


def test_a_detached_item_has_no_path(db):
    """Warehouse stock is not a place in the plant.

    Returning a one-element path for a detached item made the resolver and the
    path panel disagree: one refused with CPE_NOT_ATTACHED while the other
    rendered a single-node path. The anchor filters on network_attached so both
    read the same answer.
    """
    (core,) = _chain(db, CO_A, ["core"])
    spare = InventoryItem(
        id=uuid.uuid4(), company_id=CO_A, device_type_id=uuid.uuid4(),
        serial_number="spare", network_attached=False,
    )
    db.add(spare)
    db.flush()
    assert resolve_path(db, spare.id, CO_A) == []
