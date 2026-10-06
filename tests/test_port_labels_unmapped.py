"""doc 40 §4.2 C8: the lp1 free-text labels are unmapped (C8a, pt2 no-op) and
then dropped (C8b, pt3)."""
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


def test_pt3_drops_idempotently_on_pt2():
    pt3 = load("versions/pt3_drop_port_labels.py", "pt3_drop_port_labels")
    assert pt3.down_revision == "pt2_unmap_port_labels"
    src = open(pt3.__file__).read()
    assert "DROP INDEX IF EXISTS" in src and src.count("DROP COLUMN IF EXISTS") == 2
    assert src.count("ADD COLUMN IF NOT EXISTS") == 2 and "lock_timeout" in src
