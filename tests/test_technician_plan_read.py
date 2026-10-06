"""mp1: the revision's grants and the seed's TECHNICIAN list must agree."""
from _mi_helpers import isp_seed, load


def test_technician_gets_plan_read_in_seed_and_revision():
    mp1 = load("versions/mp1_technician_plan_read.py", "mp1_test")
    assert mp1.down_revision == "ld1_legacy_drop"
    seeded = set(isp_seed().ISP_ROLES["TECHNICIAN"]["permissions"])
    assert set(mp1.TECHNICIAN_GRANTS) <= seeded
