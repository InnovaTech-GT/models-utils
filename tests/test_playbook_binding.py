"""Playbooks bind to device types, overridable per node (doc 35 §2.4).

Binding lives in its own table rather than as a column on `playbook` so one
playbook can serve several device types — impossible under the retired
`playbook.topology_id` ownership model, where a "MikroTik core config" had to
be re-authored per chain.
"""

from database_utils.models import DeviceTypePlaybook, InventoryItemPlaybook
from database_utils.models.isp import DeviceCategory


def test_device_category_has_is_passive():
    col = DeviceCategory.__table__.c["is_passive"]
    assert col.nullable is False


def test_device_type_playbook_is_unique_per_type_and_purpose():
    names = {c.name for c in DeviceTypePlaybook.__table__.constraints if c.name}
    assert "uq_device_type_playbook_purpose" in names
    cols = set(DeviceTypePlaybook.__table__.c.keys())
    assert {"company_id", "device_type_id", "purpose", "playbook_id"} <= cols


def test_item_playbook_is_unique_per_item_and_purpose():
    names = {c.name for c in InventoryItemPlaybook.__table__.constraints if c.name}
    assert "uq_item_playbook_purpose" in names


def test_binding_playbook_fk_is_restrict_so_a_bound_playbook_cannot_vanish():
    fk = next(iter(DeviceTypePlaybook.__table__.c["playbook_id"].foreign_keys))
    assert fk.ondelete == "RESTRICT"
    fk = next(iter(InventoryItemPlaybook.__table__.c["playbook_id"].foreign_keys))
    assert fk.ondelete == "RESTRICT"


def test_item_binding_cascades_with_its_node():
    """An override is meaningless without the node it overrides."""
    fk = next(iter(InventoryItemPlaybook.__table__.c["inventory_item_id"].foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_device_type_binding_restricts_the_type():
    """A device type in use by a binding must not be deletable out from under it."""
    fk = next(iter(DeviceTypePlaybook.__table__.c["device_type_id"].foreign_keys))
    assert fk.ondelete == "RESTRICT"
