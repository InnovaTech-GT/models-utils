"""doc 70 §8 (A12, models-utils half): `record_revision` is the only
playbook_revision writer; it flushes inside the caller's transaction, so a
failing revision insert rolls the playbook write back. The connectivity
get-or-create writes CREATE, its in-place self-heal UPDATE (no actor)."""
import uuid

import pytest

from database_utils.models.isp import Playbook, PlaybookRevision
from database_utils.utils.connectivity import _get_or_create_connectivity_playbook
from database_utils.utils.playbook_revisions import record_revision

TARGET = {"scope": "device_type", "target_id": "x", "purpose": "ACTIVATION"}


class _StaffLike:
    is_staff_actor = True

    def __init__(self, uid):
        self.id = uid


class _Tenant:
    def __init__(self, uid):
        self.id = uid


def test_record_revision_fields(db, plant):
    uid = uuid.uuid4()
    pb = plant.playbooks["olt-activation"]
    rev = record_revision(db, _StaffLike(uid), pb, "BIND", TARGET)
    assert (rev.company_id, rev.action, rev.actor_staff, rev.actor_user_id) == (plant.company_id, "BIND", True, uid)
    assert (rev.playbook_id, rev.playbook_name, rev.version) == (pb.id, pb.name, pb.version)
    assert rev.definition == pb.definition and rev.target == TARGET
    assert rev.id is not None and db.query(PlaybookRevision).count() == 1  # flushed


def test_tenant_actor_is_not_staff(db, plant):
    uid = uuid.uuid4()
    rev = record_revision(db, _Tenant(uid), plant.playbooks["olt-activation"], "UPDATE")
    assert (rev.actor_staff, rev.actor_user_id, rev.target) == (False, uid, None)


@pytest.mark.parametrize("action", ["DELETE", "UNBIND"])
def test_delete_and_unbind_store_no_definition(db, plant, action):
    rev = record_revision(db, None, plant.playbooks["olt-activation"], action, TARGET if action == "UNBIND" else None)
    assert rev.definition is None and rev.actor_user_id is None and rev.actor_staff is False


def test_unknown_action_raises(db, plant):
    with pytest.raises(ValueError):
        record_revision(db, None, plant.playbooks["olt-activation"], "RENAME")
    assert db.query(PlaybookRevision).count() == 0


def test_new_playbook_gets_id_and_version_before_the_revision(db, plant):
    pb = Playbook(company_id=plant.company_id, name="fresh", definition={"steps": []})
    db.add(pb)
    rev = record_revision(db, None, pb, "CREATE")
    assert rev.playbook_id == pb.id and rev.version == 1


def test_failing_revision_insert_rolls_back_the_write(db, plant, monkeypatch):
    pb = plant.playbooks["olt-activation"]
    db.commit()
    old, new = pb.definition, {"steps": [{"name": "changed"}]}
    pb.definition = new

    def boom(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(db, "flush", boom)
    with pytest.raises(RuntimeError):
        record_revision(db, None, pb, "UPDATE")
    monkeypatch.undo()
    db.rollback()
    assert db.get(Playbook, pb.id).definition == old != new
    assert db.query(PlaybookRevision).count() == 0


def test_connectivity_create_and_self_heal_write_revisions(db):
    company_id = uuid.uuid4()
    pb = _get_or_create_connectivity_playbook(db, company_id, "ssh")
    _get_or_create_connectivity_playbook(db, company_id, "ssh")  # unchanged: no revision
    pb.definition = {"steps": [{"name": "stale"}]}
    db.flush()
    _get_or_create_connectivity_playbook(db, company_id, "ssh")  # self-heal in place
    revs = db.query(PlaybookRevision).order_by(PlaybookRevision.created_at, PlaybookRevision.action).all()
    assert [r.action for r in revs] == ["CREATE", "UPDATE"]
    assert all(r.actor_user_id is None and r.actor_staff is False for r in revs)
    assert all(r.playbook_id == pb.id and r.company_id == company_id for r in revs)
    assert revs[1].definition == pb.definition and revs[1].version == pb.version  # no version bump
