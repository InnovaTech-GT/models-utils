"""mi2 COLLECTOR grants: the revision's list and the seed's list must agree
(revisions are immutable, seeds are not — neither can import the other)."""
from _mi_helpers import isp_seed, mi2


def test_revision_grants_are_in_the_seed():
    seeded = set(isp_seed().ISP_ROLES["COLLECTOR"]["permissions"])
    assert set(mi2().COLLECTOR_GRANTS) <= seeded


def test_grants_are_existing_permission_names():
    # No new permission rows ride mi2: every grant must already be declared.
    from _mi_helpers import load
    rbac = load("seeds/rbac_seed.py", "rbac_seed_mi_test")
    declared = {p["name"] for p in rbac.PERMISSIONS_DATA}
    declared |= {p["name"] for p in isp_seed().ISP_PERMISSIONS}
    assert set(mi2().COLLECTOR_GRANTS) <= declared


def test_collector_gets_no_write_access_beyond_tasks_create():
    perms = set(isp_seed().ISP_ROLES["COLLECTOR"]["permissions"])
    writes = {p for p in perms if not p.endswith(".read")}
    assert writes == {"payments.record", "mobile.collector", "tasks.create"}
