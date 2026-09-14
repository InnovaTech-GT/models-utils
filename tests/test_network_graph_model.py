"""InventoryItem gains the network-graph columns (doc 35 §2.1).

The plant is a tree of inventory items rather than a separate node table:
everything provisioning needs (mgmt_host, credentials, ACS registration, the
device lock key) is already keyed by inventory_item.id, and a parallel node
table would re-create the network_node_type/device_type duality that doc 07
left unresolved and c2d_graph_removal ultimately deleted.
"""

from database_utils.models.isp import InventoryItem


def test_inventory_item_has_graph_columns():
    cols = InventoryItem.__table__.c
    assert "parent_id" in cols
    assert "network_attached" in cols
    assert cols["parent_id"].nullable is True
    assert cols["network_attached"].nullable is False


def test_parent_id_is_a_self_fk_with_restrict():
    """RESTRICT, not SET NULL: deleting an OLT must not silently promote every
    subscriber behind it to a root."""
    fk = next(iter(InventoryItem.__table__.c["parent_id"].foreign_keys))
    assert fk.column.table.name == "inventory_item"
    assert fk.ondelete == "RESTRICT"


def test_graph_check_constraints_are_declared():
    names = {c.name for c in InventoryItem.__table__.constraints if c.name}
    assert "ck_inventory_item_parent_attached" in names
    assert "ck_inventory_item_not_self_parent" in names


def test_parent_and_children_relationships_exist():
    assert InventoryItem.parent.property.mapper.class_ is InventoryItem
    assert InventoryItem.children.property.mapper.class_ is InventoryItem


def test_network_attached_defaults_to_false():
    """Warehouse stock is not in the graph until someone attaches it."""
    col = InventoryItem.__table__.c["network_attached"]
    assert col.default.arg is False
