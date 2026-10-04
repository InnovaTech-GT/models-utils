"""Port-level topology schema (doc 40 §3.1): templates, models, and pt1.

The Postgres-only guarantees (deferred triggers, composite FKs, NO ACTION
deletes, downgrade refusals at run time) live in tests/pg.
"""
import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from database_utils.models import isp
from database_utils.models.isp import (
    DeviceCategory,
    DeviceType,
    InventoryItem,
    InventoryItemPort,
    NetworkLink,
)
from database_utils.schemas.inventory import (
    DeviceTypeCreate,
    DeviceTypeOut,
    DeviceTypeUpdate,
    PortSpec,
    PortTemplateGroup,
    expand_port_template,
    path_role_shadows_category,
)

PT1 = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "pt1_port_topology.py"

OLT_TEMPLATE = [
    {"name": "{slot}/{n}", "slots": [1], "start": 1, "count": 16, "medium": "PON",
     "direction": "DOWN"},
    {"name": "{slot}:{n}", "slots": [9, 10], "start": 1, "count": 4, "medium": "ETH",
     "direction": "UP"},
]


def _load_pt1():
    spec = importlib.util.spec_from_file_location("pt1", PT1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ template expansion

def test_expand_the_doc_example():
    specs = expand_port_template([PortTemplateGroup(**g) for g in OLT_TEMPLATE])
    assert len(specs) == 16 + 8
    assert specs[0] == PortSpec(1, 1, "1/1", "PON", "DOWN")
    assert specs[15] == PortSpec(1, 16, "1/16", "PON", "DOWN")
    assert specs[16] == PortSpec(9, 1, "9:1", "ETH", "UP")
    assert specs[-1] == PortSpec(10, 4, "10:4", "ETH", "UP")


def test_expand_accepts_raw_json_and_slotless_groups():
    specs = expand_port_template([
        {"name": "IN", "count": 1, "medium": "PON", "direction": "UP"},
        {"name": "OUT {n}", "start": 1, "count": 16, "medium": "PON", "direction": "DOWN"},
    ])
    assert specs[0] == PortSpec(None, 1, "IN", "PON", "UP")
    assert [s.name for s in specs[1:3]] == ["OUT 1", "OUT 2"]
    assert all(s.slot is None for s in specs)


def test_placeholders_use_replace_not_format():
    # "{0}" would be a str.format index; it is rejected, never interpreted.
    with pytest.raises(ValidationError, match="only the {slot} and {n}"):
        PortTemplateGroup(name="p{0}", count=1, medium="ETH", direction="ANY")
    with pytest.raises(ValidationError, match="only the {slot} and {n}"):
        PortTemplateGroup(name="p{n.__class__}", count=1, medium="ETH", direction="ANY")


@pytest.mark.parametrize("group, needle", [
    ({"name": "{slot}/{n}", "count": 1}, "requires slots"),
    ({"name": "p{n}", "slots": [], "count": 1}, "must not be empty"),
    ({"name": "p{n}", "slots": [1, 1], "count": 1}, "unique"),
    ({"name": "p{n}", "slots": [256], "count": 1}, "between 0 and 255"),
    ({"name": "p{n}", "start": 4096, "count": 1}, "start must be"),
    ({"name": "p{n}", "start": -1, "count": 1}, "start must be"),
    ({"name": "p{n}", "count": 0}, "count must be"),
    ({"name": "p{n}", "count": 257}, "count must be"),
    ({"name": "p{n}", "start": 4000, "count": 100}, "4095"),
    ({"name": "p{n}", "count": 1, "medium": "COAX"}, "medium"),
    ({"name": "p{n}", "count": 1, "direction": "down"}, "direction"),
    ({"name": "p{n}", "count": True}, "count"),
])
def test_group_limits(group, needle):
    full = {"medium": "ETH", "direction": "ANY", **group}
    with pytest.raises(ValidationError, match=needle):
        PortTemplateGroup(**full)


def _type(template, **kw):
    return DeviceTypeCreate(name="t", port_template=template, **kw)


@pytest.mark.parametrize("name", ['p"{n}', "p'{n}", "p\n{n}", "p;{n}", "-p{n}", " p{n}",
                                  "p" * 32 + "{n}", "p{n}\\", "p`{n}`", "p$(x)"])
def test_expanded_names_must_be_cli_safe(name):
    with pytest.raises(ValidationError, match="must match"):
        _type([{"name": name, "count": 1, "medium": "ETH", "direction": "ANY"}])


@pytest.mark.parametrize("name", ["ether{n}", "sfp-sfpplus{n}", "OUT {n}", "LAN{n}",
                                  "0/{n}", "ge1.{n}", "a_b:{n}"])
def test_ordinary_names_are_accepted(name):
    _type([{"name": name, "count": 2, "medium": "ETH", "direction": "ANY"}])


def test_names_are_unique_case_insensitively():
    with pytest.raises(ValidationError, match="repeated"):
        _type([{"name": "ether{n}", "count": 2, "medium": "ETH", "direction": "ANY"},
               {"name": "ETHER{n}", "start": 2, "count": 1, "medium": "ETH",
                "direction": "ANY"}])


def test_pon_ports_are_unique_on_slot_number_direction():
    with pytest.raises(ValidationError, match="PON port slot 1 number 1"):
        _type([{"name": "a{n}", "slots": [1], "count": 1, "medium": "PON", "direction": "DOWN"},
               {"name": "b{n}", "slots": [1], "count": 1, "medium": "PON", "direction": "DOWN"}])
    # Same numbers on ETH, or on another direction, are fine.
    _type([{"name": "a{n}", "count": 2, "medium": "ETH", "direction": "ANY"},
           {"name": "b{n}", "count": 2, "medium": "ETH", "direction": "ANY"},
           {"name": "c{n}", "count": 1, "medium": "PON", "direction": "UP"},
           {"name": "d{n}", "count": 1, "medium": "PON", "direction": "DOWN"}])


def test_group_and_port_caps():
    one = {"name": "g{i}p{n}", "count": 1, "medium": "ETH", "direction": "ANY"}
    groups = [{**one, "name": f"g{i}p{{n}}"} for i in range(32)]
    _type(groups)
    with pytest.raises(ValidationError, match="at most 32 groups"):
        _type(groups + [{**one, "name": "extra{n}"}])
    big = [{"name": f"g{i}-{{slot}}/{{n}}", "slots": [0, 1], "count": 256, "medium": "ETH",
            "direction": "ANY"} for i in range(2)]
    _type(big)  # exactly 1024
    with pytest.raises(ValidationError, match="more than 1024 ports"):
        _type(big + [{"name": "x{n}", "count": 1, "medium": "ETH", "direction": "ANY"}])


def test_empty_template_means_none():
    assert _type([]).port_template is None


def test_lot_types_cannot_have_a_template():
    with pytest.raises(ValidationError, match="PORT_TEMPLATE_REQUIRES_SERIALIZED"):
        _type(OLT_TEMPLATE, is_serialized=False)
    with pytest.raises(ValidationError, match="PORT_TEMPLATE_REQUIRES_SERIALIZED"):
        DeviceTypeUpdate(port_template=OLT_TEMPLATE, is_serialized=False)
    DeviceTypeUpdate(port_template=OLT_TEMPLATE)  # the backend checks the stored flag


def test_update_distinguishes_clear_from_unset():
    assert "port_template" not in DeviceTypeUpdate().model_fields_set
    cleared = DeviceTypeUpdate(port_template=None)
    assert "port_template" in cleared.model_fields_set and cleared.port_template is None


# ------------------------------------------------------------------- path_role

@pytest.mark.parametrize("role", ["mufa_principal", "mufa_secundaria", "a", "a" * 32])
def test_valid_path_roles(role):
    assert DeviceTypeCreate(name="t", path_role=role).path_role == role


@pytest.mark.parametrize("role, needle", [
    ("Mufa", "must match"), ("1mufa", "must match"), ("mufa-x", "must match"),
    ("a" * 33, "must match"), ("mu\nfa", "must match"), ("olt_password", "secret-named"),
    ("api_key", "secret-named"),
])
def test_invalid_path_roles(role, needle):
    with pytest.raises(ValidationError, match=needle):
        DeviceTypeUpdate(path_role=role)


def test_blank_path_role_is_none():
    assert DeviceTypeCreate(name="t", path_role="  ").path_role is None


def test_path_role_shadows_category(db):
    db.add(DeviceCategory(id=uuid.uuid4(), key="OLT", name="Olt"))
    db.flush()
    assert path_role_shadows_category(db, "olt")
    assert not path_role_shadows_category(db, "mufa_principal")
    assert not path_role_shadows_category(db, None)


def test_device_type_out_carries_template_and_role(db):
    cat = DeviceCategory(id=uuid.uuid4(), key="OLT", name="Olt")
    db.add(cat)
    db.flush()
    dt = DeviceType(id=uuid.uuid4(), company_id=uuid.uuid4(), name="AN5516-04",
                    category_id=cat.id, port_template=OLT_TEMPLATE, path_role="olt_main")
    db.add(dt)
    db.flush()
    out = DeviceTypeOut.model_validate(dt)
    assert out.path_role == "olt_main"
    assert [g.model_dump() for g in out.port_template] == OLT_TEMPLATE


# ---------------------------------------------------------------- models / SQLite

def _plant(db):
    co = uuid.uuid4()
    cat = DeviceCategory(id=uuid.uuid4(), key="MUFA", name="Mufa")
    db.add(cat)
    db.flush()
    dt = DeviceType(id=uuid.uuid4(), company_id=co, name="MUFA 1:16", category_id=cat.id)
    db.add(dt)
    db.flush()
    mufa = InventoryItem(id=uuid.uuid4(), company_id=co, device_type_id=dt.id,
                         network_attached=True)
    db.add(mufa)
    db.flush()
    onu = InventoryItem(id=uuid.uuid4(), company_id=co, device_type_id=dt.id,
                        network_attached=True, parent_id=mufa.id)
    db.add(onu)
    db.flush()
    return co, dt, mufa, onu


def _port(db, co, item, name, number, medium="PON", direction="DOWN", slot=None):
    port = InventoryItemPort(id=uuid.uuid4(), company_id=co, item_id=item.id, name=name,
                             number=number, slot=slot, medium=medium, direction=direction,
                             origin="TEMPLATE")
    db.add(port)
    db.flush()
    return port


def test_ports_and_links_work_under_sqlite_create_all(db):
    co, _, mufa, onu = _plant(db)
    out6 = _port(db, co, mufa, "OUT 6", 6)
    pon = _port(db, co, onu, "PON", 1, direction="UP")
    link = NetworkLink(id=uuid.uuid4(), company_id=co, up_item_id=mufa.id, up_port_id=out6.id,
                       down_item_id=onu.id, down_port_id=pon.id, source="FIELD")
    db.add(link)
    db.flush()
    db.expire_all()
    assert [p.name for p in mufa.ports] == ["OUT 6"]
    assert onu.uplink.up_port.name == "OUT 6"
    assert onu.uplink.down_port.name == "PON"
    assert onu.uplink.down_item.id == onu.id
    assert out6.item.id == mufa.id


def test_sqlite_enforces_the_expression_indexes(db):
    co, _, mufa, _ = _plant(db)
    _port(db, co, mufa, "OUT 1", 1)
    with pytest.raises(sa.exc.IntegrityError):
        _port(db, co, mufa, "out 1", 2)                       # uq_item_port_name
    db.rollback()


def test_sqlite_pon_index_is_partial(db):
    co, _, mufa, _ = _plant(db)
    _port(db, co, mufa, "ether1", 1, medium="ETH")
    _port(db, co, mufa, "sfp1", 1, medium="ETH")              # ETH numbers may repeat
    _port(db, co, mufa, "OUT 1", 1)
    with pytest.raises(sa.exc.IntegrityError):
        _port(db, co, mufa, "OUT 1b", 1)                      # uq_item_port_pon_number
    db.rollback()


def test_explicit_none_template_is_sql_null(db):
    """none_as_null: otherwise JSON 'null' trips ck_device_type_ports_serialized."""
    cat = DeviceCategory(id=uuid.uuid4(), key="FIBER", name="Fiber")
    db.add(cat)
    db.flush()
    dt = DeviceType(id=uuid.uuid4(), company_id=uuid.uuid4(), name="drop", category_id=cat.id,
                    is_serialized=False, port_template=None)
    db.add(dt)
    db.flush()
    raw = db.execute(sa.text("SELECT port_template IS NULL FROM device_type WHERE name='drop'"))
    assert raw.scalar() == 1


def test_lot_type_with_template_violates_the_check(db):
    cat = DeviceCategory(id=uuid.uuid4(), key="FIBER", name="Fiber")
    db.add(cat)
    db.flush()
    db.add(DeviceType(id=uuid.uuid4(), company_id=uuid.uuid4(), name="drop",
                      category_id=cat.id, is_serialized=False, port_template=OLT_TEMPLATE))
    with pytest.raises(sa.exc.IntegrityError):
        db.flush()
    db.rollback()


def test_triggers_are_not_in_the_metadata():
    from sqlalchemy.schema import CreateTable

    from database_utils.database import Base
    ddl = " ".join(str(CreateTable(t)) for t in Base.metadata.sorted_tables)
    assert "network_link_assert_parent" not in ddl
    assert "plpgsql" not in ddl
    assert "CONSTRAINT TRIGGER" not in ddl


# -------------------------------------------------------------------------- pt1

def test_pt1_chains_to_the_current_head():
    mod = _load_pt1()
    assert mod.revision == "pt1_port_topology"
    assert len(mod.revision) <= 32
    assert mod.down_revision == "sh1_service_history_repair"
    downs = set()
    for path in PT1.parent.glob("*.py"):
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("down_revision"):
                downs.add(line)
    assert not any("pt1_port_topology" in d for d in downs), "pt1 must be the head"


def test_pt1_is_additive():
    body = PT1.read_text()
    upgrade = body.split("def upgrade")[1].split("def downgrade")[0]
    for forbidden in ("DROP TABLE", "DROP COLUMN", "drop_table", "drop_column"):
        assert forbidden not in upgrade
    assert "SET lock_timeout = '5s'" in upgrade


def test_pt1_installs_both_deferred_triggers():
    mod = _load_pt1()
    body = PT1.read_text()
    for name in ("trg_network_link_parent_sync", "trg_inventory_item_link_sync"):
        block = body.split(f"CREATE CONSTRAINT TRIGGER {name}")[1].split('"""')[0]
        assert "DEFERRABLE INITIALLY DEFERRED" in block, name
    assert "AFTER UPDATE OF parent_id ON inventory_item" in body
    assert "NETWORK_LINK_PARENT_MISMATCH" in mod._ASSERT_PARENT_FN
    assert "IS DISTINCT FROM l.up_item_id" in mod._ASSERT_PARENT_FN


def test_pt1_creates_the_designed_constraints():
    body = PT1.read_text()
    for name in ("ck_device_type_ports_serialized", "uq_inventory_item_id_company",
                 "fk_item_port_item", "uq_item_port_identity", "uq_item_port_name",
                 "uq_item_port_pon_number", "ix_item_port_company", "fk_link_up_port",
                 "fk_link_down_item", "fk_link_down_port", "uq_link_up_port",
                 "uq_link_down_port", "uq_link_down_item", "ck_link_not_self",
                 "ix_network_link_up_item", "ix_network_link_company"):
        assert name in body, name
    assert "(item_id, lower(name))" in body
    assert "(item_id, coalesce(slot, -1), number, direction) WHERE medium = 'PON'" in body
    # NO ACTION on both port FKs: neither carries an ON DELETE clause.
    for fk in ("fk_link_up_port", "fk_link_down_port"):
        clause = body.split(f"CONSTRAINT {fk}")[1].split("CONSTRAINT")[0]
        assert "ON DELETE" not in clause, fk


def test_pt1_vocabularies_match_the_models():
    mod = _load_pt1()
    assert mod.PORT_MEDIA == isp.PORT_MEDIA
    assert mod.PORT_DIRECTIONS == isp.PORT_DIRECTIONS
    assert mod.PORT_ORIGINS == isp.PORT_ORIGINS
    assert mod.NETWORK_LINK_SOURCES == isp.NETWORK_LINK_SOURCES


def test_pt1_downgrade_refuses_while_links_or_item_ports_exist():
    """Static half; tests/pg runs the refusal against a real database."""
    body = PT1.read_text().split("def downgrade")[1]
    assert "SELECT count(*) FROM network_link" in body
    assert "origin = 'ITEM'" in body
    assert body.index("refusing downgrade") < body.index("DROP TABLE")


class _FakeConnection:
    def __init__(self, links, item_ports):
        self.counts = iter([links, item_ports])

    def execute(self, statement, *_args, **_kwargs):
        value = next(self.counts) if "count(*)" in str(statement) else None
        return type("R", (), {"scalar": lambda self: value})()


@pytest.mark.parametrize("links, item_ports", [(1, 0), (0, 3)])
def test_pt1_downgrade_raises_before_any_ddl(monkeypatch, links, item_ports):
    mod = _load_pt1()
    executed = []
    monkeypatch.setattr(mod.op, "get_bind", lambda: _FakeConnection(links, item_ports),
                        raising=False)
    monkeypatch.setattr(mod.op, "execute", executed.append, raising=False)
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        mod.downgrade()
    assert executed == []
