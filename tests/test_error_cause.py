"""doc 70 §6.1: the one definition of "a code" (error_cause / job_error_cause),
the reduced provisioning_job trigger snapshot and ProvisioningJobOut's cause."""
import uuid

import pytest

from database_utils.models.isp import ProvisioningJob, ProvisioningJobStatus
from database_utils.schemas.playbook import ProvisioningJobOut
from database_utils.utils.provisioning_jobs import error_cause, job_error_cause, reduce_job_snapshot

STEP_ERR = "step 'probe': CONNECT_TIMEOUT: ssh failed against 192.0.2.10:22: timed out"   # the worker's real shape
SENTINEL = "sentinel-raw-device-output"
SNAP = {
    "id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "playbook_id": str(uuid.uuid4()),
    "status": "FAILED", "attempts": 2, "max_attempts": 3, "dry_run": False, "client_service_id": None,
    "inventory_item_id": str(uuid.uuid4()), "triggered_by": "USER", "run_id": str(uuid.uuid4()),
    "error": f"ssh: {SENTINEL}",
    "variables": {"wifi_password": SENTINEL},
    "log": {"steps": [{"name": "s", "rendered": SENTINEL, "output": SENTINEL}], "variables": {"x": SENTINEL}},
}
KEPT = [k for k in SNAP if k not in ("log", "variables", "error")]


@pytest.mark.parametrize("text,code", [
    (None, None), ("", None), ("PROVISIONING_DISABLED", "PROVISIONING_DISABLED"),
    ("CONNECT_TIMEOUT: 192.0.2.10:22 timed out", "CONNECT_TIMEOUT"), (" AUTH_FAILED : x", "AUTH_FAILED"),
    ("ssh: connection refused", "PROVISIONING_FAILED"), ("Timeout: x", "PROVISIONING_FAILED"),
    ("sentinel raw failure text", "PROVISIONING_FAILED"), ("1BAD: x", "PROVISIONING_FAILED"),
    (STEP_ERR, "PROVISIONING_FAILED"), ("A", "A"), ("A-B: x", "PROVISIONING_FAILED"),
])
def test_error_cause(text, code):
    assert error_cause(text) == code


@pytest.mark.parametrize("error,log,code", [
    (STEP_ERR, {"code": "CONNECT_TIMEOUT"}, "CONNECT_TIMEOUT"),   # log.code wins
    (STEP_ERR, None, "PROVISIONING_FAILED"),                       # no log.code: the head is not code-shaped
    (STEP_ERR, {"code": "not a code"}, "PROVISIONING_FAILED"),
    ("PROVISIONING_DISABLED", {}, "PROVISIONING_DISABLED"), (None, None, None),
    (None, {"code": "AUTH_FAILED"}, "AUTH_FAILED"), (None, {"steps": []}, None),
    ("AUTH_FAILED: x", "not a dict", "AUTH_FAILED"), (STEP_ERR, {"code": None}, "PROVISIONING_FAILED"),
])
def test_job_error_cause(error, log, code):
    assert job_error_cause(error, log) == code


def test_reduce_job_snapshot_keeps_templatable_columns():
    out = reduce_job_snapshot(SNAP)   # SNAP has log (no "code"), variables, error="ssh: x", playbook_id, ...
    assert "log" not in out and "variables" not in out and out["error"] == "PROVISIONING_FAILED"
    assert {k: out[k] for k in KEPT} == {k: SNAP[k] for k in KEPT}
    assert SENTINEL not in repr(out)
    assert "log" in SNAP and SNAP["error"] == f"ssh: {SENTINEL}"   # the input is not mutated
    assert reduce_job_snapshot({**SNAP, "error": STEP_ERR, "log": {"code": "AUTH_FAILED"}})["error"] == "AUTH_FAILED"
    assert reduce_job_snapshot({**SNAP, "error": None, "log": None})["error"] is None
    assert reduce_job_snapshot(None) is None


def _job(db, plant, error=None, log=None):
    job = ProvisioningJob(id=uuid.uuid4(), company_id=plant.company_id,
                          playbook_id=plant.playbooks["olt-activation"].id, variables={},
                          status=ProvisioningJobStatus.FAILED, error=error, log=log)
    db.add(job)
    db.flush()
    db.refresh(job)
    return job


def test_job_out_fills_playbook_name_and_cause_before_projection(db, plant):
    out = ProvisioningJobOut.model_validate(_job(db, plant, error="AUTH_FAILED: denied"))
    assert out.playbook_name == plant.playbooks["olt-activation"].name and out.error_cause == "AUTH_FAILED"
    # what FastAPI's response validation does after the backend's projection: dump, then validate again
    again = ProvisioningJobOut.model_validate(
        out.model_dump() | {"error": "La configuración automática está deshabilitada …",
                            "log": {"steps": []}, "playbook": None})
    assert again.error_cause == "AUTH_FAILED" and again.playbook_name == out.playbook_name
    real = ProvisioningJobOut.model_validate(_job(db, plant, error=STEP_ERR, log={"code": "CONNECT_TIMEOUT", "steps": []}))
    assert real.error_cause == "CONNECT_TIMEOUT"
    clean = ProvisioningJobOut.model_validate(_job(db, plant))
    assert clean.error_cause is None and clean.playbook_name == plant.playbooks["olt-activation"].name
