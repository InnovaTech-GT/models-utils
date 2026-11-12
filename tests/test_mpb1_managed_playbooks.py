"""mpb1: Uplink-managed playbooks schema (doc 70 §7.1, §10)."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models import Company, PlaybookRevision, ProvisioningRun
from database_utils.models.isp import PLAYBOOK_REVISION_ACTIONS
from database_utils.schemas.company import (
    AdminCompanyOut, CompanyOut, CompanyUpdate, PlaybookManagement, PlaybookManagementDecisionIn,
    PlaybookManagementOut, PlaybookManagementRequestIn,
)
from database_utils.schemas.playbook import (
    DeclaredInputOut, PlaybookRevisionDetailOut, PlaybookRevisionOut, PlaybookSimulateIn,
)


def _pm(**kw):
    base = {"status": "PENDING", "reason": "we have our own NOC", "requested_by": str(uuid.uuid4()),
            "requested_at": "2026-11-11T10:00:00-06:00", "ack_version": "2026-11-11",
            "decided_by": None, "decided_at": None, "note": None}
    return base | kw


def _company_obj(**kw):
    base = dict(id=uuid.uuid4(), name="ISP", email=None, phone=None, active=True,
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc), tier_id=uuid.uuid4(),
                tax_id=None, address="", mobile_settings=None, dunning_settings=None,
                playbook_management=None)
    return SimpleNamespace(**(base | kw))


def test_mpb1_revision_chain():
    # down_revision is re-pointed at compose, with this assertion (doc 70a Compose handoff)
    m = load("versions/mpb1_managed_playbooks.py", "mpb1_managed_playbooks")
    assert m.revision == "mpb1_managed_playbooks" and m.down_revision == "et1_edge_telnet"
    assert len(m.revision) <= 32


def test_columns_and_table():
    assert "playbook_management" in Company.__table__.c
    sa_col = ProvisioningRun.__table__.c.staff_actor
    assert sa_col.nullable is False and "false" in str(sa_col.server_default.arg)
    t = PlaybookRevision.__table__
    assert t.name == "playbook_revision"
    assert not t.c.playbook_id.foreign_keys and t.c.playbook_id.nullable is False
    assert {fk.ondelete for fk in t.c.company_id.foreign_keys} == {"CASCADE"}
    assert {fk.ondelete for fk in t.c.actor_user_id.foreign_keys} == {"SET NULL"}
    assert t.c.actor_staff.nullable is False and t.c.definition.nullable is True
    assert {i.name for i in t.indexes} >= {"ix_playbook_revision_company_created", "ix_playbook_revision_playbook"}
    assert PLAYBOOK_REVISION_ACTIONS == ("BASELINE", "CREATE", "UPDATE", "DELETE", "BIND", "UNBIND")


def test_management_schema_bounds():
    assert PlaybookManagement(**_pm()).status == "PENDING"
    with pytest.raises(ValidationError):
        PlaybookManagement(**_pm(reason=""))
    with pytest.raises(ValidationError):
        PlaybookManagement(**_pm(reason="x" * 1001))
    with pytest.raises(ValidationError):
        PlaybookManagement(**_pm(status="approved"))
    with pytest.raises(ValidationError):
        PlaybookManagement(**_pm(note="x" * 1001))
    with pytest.raises(ValidationError):
        PlaybookManagement(**_pm(extra="no"))


def test_request_in_requires_acceptance_and_real_reason():
    for body in ({"reason": "x"}, {"reason": "x", "accept_responsibility": False},
                 {"reason": "   ", "accept_responsibility": True},
                 {"reason": "x" * 1001, "accept_responsibility": True}):
        with pytest.raises(ValidationError):
            PlaybookManagementRequestIn(**body)
    assert PlaybookManagementRequestIn(reason=" x ", accept_responsibility=True).reason == "x"


def test_decision_in():
    assert PlaybookManagementDecisionIn(decision="APPROVE").note is None
    for body in ({"decision": "approve"}, {"decision": "CANCEL"}, {"decision": "DENY", "note": "x" * 1001}):
        with pytest.raises(ValidationError):
            PlaybookManagementDecisionIn(**body)


def test_management_out_defaults():
    out = PlaybookManagementOut(self_managed=False)
    assert out.status is None and out.reason is None


def test_management_not_settable_through_company_update():
    assert "playbook_management" not in CompanyUpdate.model_fields
    assert "playbook_management" not in CompanyOut.model_fields


def test_admin_company_out_tolerates_partial_management():
    for stored in ({"status": "APPROVED"}, "APPROVED", {}, None):
        out = AdminCompanyOut.model_validate(_company_obj(playbook_management=stored))
        assert out.playbook_management is None
        assert out.playbook_management_status == "UPLINK" and out.playbooks_unsimulated == 0
    out = AdminCompanyOut.model_validate(_company_obj(playbook_management=_pm(status="PENDING")))
    assert out.playbook_management.status == "PENDING"


def test_revision_and_simulate_schemas():
    row = SimpleNamespace(id=uuid.uuid4(), playbook_id=uuid.uuid4(), playbook_name="OLT", version=2,
                          action="UPDATE", target=None, actor_name=None, actor_staff=True,
                          created_at=datetime.now(timezone.utc), definition={"a": 1})
    assert "definition" not in PlaybookRevisionOut.model_validate(row).model_dump()
    d = PlaybookRevisionDetailOut.model_validate(row)
    assert d.definition == {"a": 1} and d.previous_definition is None
    assert PlaybookSimulateIn(client_service_id=uuid.uuid4()).purpose == "ACTIVATION"
    assert PlaybookSimulateIn(client_service_id=uuid.uuid4(), purpose=" suspension ").purpose == "SUSPENSION"
    with pytest.raises(ValidationError):
        PlaybookSimulateIn(client_service_id=uuid.uuid4(), purpose="bad purpose!")
    assert DeclaredInputOut(key="wifi", type="TEXT", required=True).label is None
