"""NAT transport (spec §8) revisions: chain-position guardrails only.

nat1/nat2/nat3 shipped `network_access.gateway_host`, `pylon_socks5` and their
CHECKs. `tr1_transport_axis` dropped that table, so the model constants these
tests used to pin byte-for-byte against each revision's copy no longer exist —
those assertions moved to tests/test_transport_axis.py, against tr1's fragments.

Revisions are IMMUTABLE and these three stay in the chain forever (a fresh
database still migrates through them on its way to tr1), so what is still worth
pinning is exactly that: each one's position, and the 32-character id limit.
The `_NAT_PORT_CHECK`/`_MGMT_PORT_CHECK` fragments DO survive — they constrain
inventory_item, not network_access.
"""
import importlib.util
import os

from database_utils.models import isp

_VERSIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _load_migration(filename, module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_VERSIONS_DIR, filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_nat1():
    return _load_migration("nat1_gateway_transport.py", "nat1_gateway_transport")


def _load_nat2():
    return _load_migration("nat2_gateway_host_check.py", "nat2_gateway_host_check")


def _load_nat3():
    return _load_migration("nat3_pylon_socks5.py", "nat3_pylon_socks5")




def test_the_item_port_fragments_survive_the_table_drop():
    """nat1 also added the range CHECKs `inventory_item` had lacked since nc2a.
    `inventory_item` is untouched by tr1, so these two still have a model side to
    be pinned against."""
    nat1 = _load_nat1()
    assert nat1._NAT_PORT_CHECK == isp._NAT_PORT_CHECK
    assert nat1._MGMT_PORT_CHECK == isp._MGMT_PORT_CHECK


def test_migration_chain_position():
    nat1 = _load_nat1()
    assert nat1.revision == "nat1_gateway_transport"
    assert nat1.down_revision == "ng2_topology_drop"
    assert len(nat1.revision) <= 32


def test_nat2_migration_chain_position():
    nat2 = _load_nat2()
    assert nat2.revision == "nat2_gateway_host_check"
    assert nat2.down_revision == "nat1_gateway_transport"
    assert len(nat2.revision) <= 32


def test_nat3_migration_chain_position():
    nat3 = _load_nat3()
    assert nat3.revision == "nat3_pylon_socks5"
    assert nat3.down_revision == "nat2_gateway_host_check"
    assert len(nat3.revision) <= 32
