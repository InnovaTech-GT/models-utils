"""Guided provisioning runs (doc 50, revision gr1): run mode + pause timeout."""

import pytest
from pydantic import ValidationError

from database_utils.models import ProvisioningRun
from database_utils.models.isp import PURPOSE_ACTIVATION, ProvisioningRunMode
from database_utils.schemas.client_service import ClientServiceProvisionIn
from database_utils.utils.provisioning_runs import (
    GUIDED_TIMEOUT_DEFAULT,
    GUIDED_TIMEOUT_MAX,
    GUIDED_TIMEOUT_MIN,
    create_or_get_run,
    create_run,
)


def test_constants():
    assert (GUIDED_TIMEOUT_MIN, GUIDED_TIMEOUT_DEFAULT, GUIDED_TIMEOUT_MAX) == (300, 1800, 14400)
    assert [m.value for m in ProvisioningRunMode] == ["AUTOMATIC", "GUIDED"]


def test_columns_have_server_defaults():
    c = ProvisioningRun.__table__.c
    assert not c["mode"].nullable and c["mode"].server_default.arg == "AUTOMATIC"
    assert not c["guided_timeout_seconds"].nullable
    assert c["guided_timeout_seconds"].server_default.arg == "1800"


def test_create_run_defaults_to_automatic(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert run.mode == "AUTOMATIC"
    assert run.guided_timeout_seconds == 1800


def test_create_run_stores_guided_mode(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION,
                     mode=ProvisioningRunMode.GUIDED, guided_timeout_seconds=600)
    db.refresh(run)
    assert run.mode == "GUIDED"
    assert run.guided_timeout_seconds == 600


def test_create_or_get_run_forwards_mode(db, plant):
    run, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION,
                                     mode="GUIDED", guided_timeout_seconds=300)
    assert created and run.mode == "GUIDED" and run.guided_timeout_seconds == 300


@pytest.mark.parametrize("kw", [
    {"mode": "STEPWISE"},
    {"guided_timeout_seconds": 299},
    {"guided_timeout_seconds": 14401},
])
def test_create_run_rejects_bad_values(db, plant, kw):
    with pytest.raises(ValueError):
        create_run(db, plant.service, PURPOSE_ACTIVATION, **kw)
    assert db.query(ProvisioningRun).count() == 0, "nothing written before validation"


def test_provision_in_defaults():
    body = ClientServiceProvisionIn()
    assert body.mode == "AUTOMATIC" and body.guided_timeout_seconds is None


@pytest.mark.parametrize("t", [299, 14401])
def test_provision_in_rejects_timeout_out_of_range(t):
    with pytest.raises(ValidationError):
        ClientServiceProvisionIn(mode="GUIDED", guided_timeout_seconds=t)


def test_provision_in_accepts_guided():
    body = ClientServiceProvisionIn(mode="GUIDED", guided_timeout_seconds=300)
    assert body.mode == "GUIDED" and body.guided_timeout_seconds == 300
    with pytest.raises(ValidationError):
        ClientServiceProvisionIn(mode="STEPWISE")


def test_create_run_none_timeout_means_default(db, plant):
    # ClientServiceProvisionIn.guided_timeout_seconds is Optional (None = the
    # run default), so a caller forwarding the body field as-is must not 500.
    run = create_run(db, plant.service, PURPOSE_ACTIVATION,
                     mode="GUIDED", guided_timeout_seconds=None)
    assert run.guided_timeout_seconds == GUIDED_TIMEOUT_DEFAULT
