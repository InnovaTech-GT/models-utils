"""Run triggers (doc 68 §3): the ProvisioningTrigger labels, the rtg1 revision,
the USER→MCP swap at run/job creation and DeviceActionLog.actor_kind."""
import inspect
import re
import uuid

import pytest
from _mi_helpers import load

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    DeviceActionLog,
    ProvisioningJob,
    ProvisioningJobStatus,
)
from database_utils.models.isp import (
    ProvisioningTrigger as T,
)
from database_utils.utils import provisioning_runs
from database_utils.utils.connectivity import enqueue_connectivity_check
from database_utils.utils.logging_utils import request_client_context
from database_utils.utils.provisioning_jobs import write_device_action_log
from database_utils.utils.provisioning_runs import (
    create_or_get_run,
    create_run,
    effective_trigger,
)


def rtg1():
    return load("versions/rtg1_run_triggers.py", "rtg1_run_triggers")


def test_trigger_values():
    assert [t.value for t in T] == ["USER", "WORKFLOW", "API", "MCP", "BOT", "AUTO_SUSPENSION", "AUTO_REACTIVATION"]


def test_rtg1_revision_chain():
    m = rtg1()
    assert m.revision == "rtg1_run_triggers" and m.down_revision == "et1_edge_telnet"


def test_rtg1_labels_match_the_python_enum():
    assert {T(v) for v in rtg1().NEW_LABELS} == {T.MCP, T.BOT, T.AUTO_SUSPENSION, T.AUTO_REACTIVATION}


def test_rtg1_rewrites_no_row():
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", inspect.getsource(rtg1().upgrade), re.IGNORECASE)


class _Span:
    def __init__(self):
        self.attrs = {}

    def is_recording(self):
        return True

    def set_attribute(self, k, v):
        self.attrs[k] = v


@pytest.fixture
def mcp_request():
    token = request_client_context.set("mcp")
    yield
    request_client_context.reset(token)


@pytest.mark.parametrize("t", list(T))
def test_effective_trigger_swaps_only_user_in_an_mcp_request(t, mcp_request):
    assert effective_trigger(t) is (T.MCP if t is T.USER else t)


@pytest.mark.parametrize("t", list(T))
def test_effective_trigger_is_identity_outside_an_mcp_request(t):
    assert effective_trigger(t) is t


def test_create_run_in_an_mcp_request_is_mcp_with_its_child(db, plant, mcp_request):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    child = db.query(ProvisioningJob).filter_by(run_id=run.id).one()
    assert run.triggered_by is T.MCP and child.triggered_by is T.MCP


def test_create_run_keeps_an_explicit_automatic_value_in_an_mcp_request(db, plant, mcp_request):
    assert create_run(db, plant.service, PURPOSE_ACTIVATION,
                      triggered_by=T.AUTO_REACTIVATION).triggered_by is T.AUTO_REACTIVATION


def test_create_run_sets_the_span_attributes(db, plant, mcp_request, monkeypatch):
    span = _Span()
    monkeypatch.setattr(provisioning_runs.trace, "get_current_span", lambda: span)
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert span.attrs == {"provisioning.trigger": "MCP", "provisioning.run_id": str(run.id)}


def test_dedupe_hit_keeps_the_first_creators_trigger(db, plant):  # Review Focus 3
    first, _ = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION, triggered_by=T.AUTO_SUSPENSION)
    token = request_client_context.set("mcp")
    try:
        again, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION)
    finally:
        request_client_context.reset(token)
    assert (again.id, created, again.triggered_by) == (first.id, False, T.AUTO_SUSPENSION)


def test_connectivity_check_in_an_mcp_request(db, plant, mcp_request):
    user = enqueue_connectivity_check(db, plant.core, idempotency_key=f"u-{uuid.uuid4()}", triggered_by=T.USER)
    sweep = enqueue_connectivity_check(db, plant.core, idempotency_key=f"s-{uuid.uuid4()}")
    assert (user.triggered_by, sweep.triggered_by) == (T.MCP, T.API)


@pytest.mark.parametrize("t,kind", [(T.USER, "user"), (T.MCP, "user"), (T.WORKFLOW, "automation"),
    (T.API, "automation"), (T.BOT, "automation"), (T.AUTO_SUSPENSION, "automation"),
    (T.AUTO_REACTIVATION, "automation")])
def test_actor_kind_by_trigger(db, plant, t, kind):  # A3.3
    job = ProvisioningJob(company_id=plant.company_id, playbook_id=plant.playbooks["onu-activation"].id,
                          triggered_by=t, status=ProvisioningJobStatus.SUCCEEDED, dry_run=False,
                          log={"steps": [{"driver": "tr069", "name": "set"}]})
    db.add(job)
    db.flush()
    write_device_action_log(db, job)
    assert db.query(DeviceActionLog).filter_by(provisioning_job_id=job.id).one().actor_kind == kind
