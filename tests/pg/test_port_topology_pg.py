"""pt1_port_topology guarantees that only Postgres can prove (doc 40 §3.1.1).

Run against a database already at `alembic upgrade head`:

    PG_TEST_URL=postgresql://erp:erp@localhost:5432/<throwaway> pytest -m pg tests/pg

Skipped when PG_TEST_URL is unset, so the SQLite suite (`pytest -v`) is
unaffected. Every test runs in one transaction that is rolled back; the
deferred triggers are fired with SET CONSTRAINTS ALL IMMEDIATE instead of a
COMMIT.
"""
import importlib.util
import os
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

PT1 = pathlib.Path(__file__).parents[2] / "alembic" / "versions" / "pt1_port_topology.py"


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _company(conn):
    cid = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=cid, name=f"pt-test-{cid}")
    return cid


def _type(conn, company_id, serialized=True):
    tid = uuid.uuid4()
    _x(conn, "INSERT INTO device_type (id, created_at, updated_at, name, company_id, "
             "category_id, is_serialized) SELECT :id, now(), now(), 'pt-type', :co, id, :ser "
             "FROM device_category LIMIT 1", id=tid, co=company_id, ser=serialized)
    return tid


def _item(conn, company_id, type_id, parent=None):
    iid = uuid.uuid4()
    _x(conn, "INSERT INTO inventory_item (id, created_at, updated_at, company_id, "
             "device_type_id, network_attached, parent_id) "
             "VALUES (:id, now(), now(), :co, :t, true, :p)",
             id=iid, co=company_id, t=type_id, p=parent)
    return iid


def _port(conn, company_id, item_id, name, number, direction="DOWN", origin="TEMPLATE"):
    pid = uuid.uuid4()
    _x(conn, "INSERT INTO inventory_item_port (id, company_id, item_id, name, number, "
             "medium, direction, origin, created_at, updated_at) "
             "VALUES (:id, :co, :item, :name, :n, 'ETH', :d, :o, now(), now())",
             id=pid, co=company_id, item=item_id, name=name, n=number, d=direction, o=origin)
    return pid


def _link(conn, company_id, up_item, up_port, down_item, down_port=None):
    _x(conn, "INSERT INTO network_link (id, company_id, up_item_id, up_port_id, "
             "down_item_id, down_port_id, source, created_at, updated_at) "
             "VALUES (:id, :co, :ui, :up, :di, :dp, 'OFFICE', now(), now())",
             id=uuid.uuid4(), co=company_id, ui=up_item, up=up_port, di=down_item,
             dp=down_port)


def _flush_deferred(conn):
    _x(conn, "SET CONSTRAINTS ALL IMMEDIATE")


@pytest.fixture()
def plant(conn):
    co = _company(conn)
    t = _type(conn, co)
    mufa = _item(conn, co, t)
    onu = _item(conn, co, t, parent=mufa)
    out1 = _port(conn, co, mufa, "OUT 1", 1)
    pon = _port(conn, co, onu, "PON", 1, direction="UP")
    return {"co": co, "type": t, "mufa": mufa, "onu": onu, "out1": out1, "pon": pon}


def test_a_consistent_link_passes_the_deferred_check(conn, plant):
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"], p["pon"])
    _flush_deferred(conn)


def test_a_bypass_writer_fails_with_parent_mismatch(conn, plant):
    p = plant
    other = _item(conn, p["co"], p["type"])
    port = _port(conn, p["co"], other, "OUT 9", 9)
    _link(conn, p["co"], other, port, p["onu"])       # onu hangs from mufa, not other
    with pytest.raises(DBAPIError, match="NETWORK_LINK_PARENT_MISMATCH"):
        _flush_deferred(conn)


def test_reparenting_without_moving_the_link_fails(conn, plant):
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"])
    _flush_deferred(conn)
    _x(conn, "SET CONSTRAINTS ALL DEFERRED")
    other = _item(conn, p["co"], p["type"])
    _x(conn, "UPDATE inventory_item SET parent_id = :o WHERE id = :i", o=other, i=p["onu"])
    with pytest.raises(DBAPIError, match="NETWORK_LINK_PARENT_MISMATCH"):
        _flush_deferred(conn)


def test_a_cross_tenant_link_is_rejected_by_the_composite_fk(conn, plant):
    p = plant
    other_co = _company(conn)
    other_onu = _item(conn, other_co, _type(conn, other_co))
    with pytest.raises(DBAPIError, match="fk_link_down_item"):
        _link(conn, p["co"], p["mufa"], p["out1"], other_onu)


def test_a_link_naming_another_items_port_is_rejected(conn, plant):
    p = plant
    other = _item(conn, p["co"], p["type"])
    with pytest.raises(DBAPIError, match="fk_link_up_port"):
        _link(conn, p["co"], other, p["out1"], p["onu"])  # out1 belongs to mufa


def test_deleting_a_linked_leaf_onu_succeeds(conn, plant):
    """NO ACTION on fk_link_down_port is checked after the cascades, by which
    time the cascade has removed the link."""
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"], p["pon"])
    _x(conn, "DELETE FROM inventory_item WHERE id = :i", i=p["onu"])
    _flush_deferred(conn)
    assert _x(conn, "SELECT count(*) FROM network_link WHERE company_id = :c",
              c=p["co"]).scalar() == 0


def test_deleting_only_a_linked_down_port_fails(conn, plant):
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"], p["pon"])
    with pytest.raises(DBAPIError, match="fk_link_down_port"):
        _x(conn, "DELETE FROM inventory_item_port WHERE id = :i", i=p["pon"])


def test_company_delete_cascades_through_ports_and_links(conn, plant):
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"], p["pon"])
    _x(conn, "DELETE FROM company WHERE id = :c", c=p["co"])
    _flush_deferred(conn)
    for table in ("inventory_item_port", "network_link", "inventory_item"):
        assert _x(conn, f"SELECT count(*) FROM {table} WHERE company_id = :c",
                  c=p["co"]).scalar() == 0


def test_port_names_are_unique_case_insensitively(conn, plant):
    p = plant
    with pytest.raises(DBAPIError, match="uq_item_port_name"):
        _port(conn, p["co"], p["mufa"], "out 1", 2)


def test_lot_types_cannot_carry_a_port_template(conn, plant):
    with pytest.raises(DBAPIError, match="ck_device_type_ports_serialized"):
        _x(conn, "UPDATE device_type SET port_template = '[]'::json, is_serialized = false "
                 "WHERE id = :t", t=plant["type"])


def _downgrade(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("pt1_pg", PT1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with Operations.context(MigrationContext.configure(conn)):
        mod.downgrade()


def test_downgrade_refuses_while_links_exist(conn, plant):
    p = plant
    _link(conn, p["co"], p["mufa"], p["out1"], p["onu"])
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        _downgrade(conn)


def test_downgrade_refuses_while_item_ports_exist(conn, plant):
    _port(conn, plant["co"], plant["mufa"], "EXTRA", 50, origin="ITEM")
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        _downgrade(conn)
