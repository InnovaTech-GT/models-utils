"""doc 40 §4.2 C8a: the lp1 free-text labels are unmapped (the DB keeps them
until C8b), and pt2 is a no-op on the current head."""
from _mi_helpers import load

from database_utils.models.isp import InventoryItem


def test_labels_and_their_index_are_unmapped():
    table = InventoryItem.__table__
    assert "parent_port" not in table.c and "uplink_port" not in table.c
    assert "uq_inventory_item_parent_port" not in {i.name for i in table.indexes}
    assert not hasattr(InventoryItem, "parent_port")


def test_pt2_is_a_noop_on_vw1():
    pt2 = load("versions/pt2_unmap_port_labels.py", "pt2_unmap_port_labels")
    assert pt2.down_revision == "vw1_viewer_no_credential_read"
    assert pt2.upgrade() is None and pt2.downgrade() is None
