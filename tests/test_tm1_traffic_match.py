"""tm1 (doc 74): the traffic match template, its settings and the shared
service variables it renders over."""
import uuid
from types import SimpleNamespace

import pytest
from _mi_helpers import load
from pydantic import ValidationError

from database_utils.models import TRAFFIC_SETTINGS_DEFAULTS, ClientService, InventoryItem
from database_utils.schemas.client_service import ClientServiceUpdate
from database_utils.schemas.company import TrafficSettings
from database_utils.utils.provisioning_resolution import service_variables
from database_utils.utils.traffic_match import check_match_template, render_match_key

CSR = "<pppoe-{{client.code | alnum}}>"


def test_csr_template_is_valid_and_renders_the_queue_name():
    check_match_template(CSR)
    assert render_match_key(CSR, {"client.code": "CO0764"}) == "<pppoe-CO0764>"


@pytest.mark.parametrize("bad,code", [
    ("pppoe-juan", "TEMPLATE_NO_TOKEN"),
    ("{{capture.ppp_user}}", "TEMPLATE_NAMESPACE"),
    ("{{secret.pppoe_password}}", "TEMPLATE_NAMESPACE"),
    ("{{input.vlan}}", "TEMPLATE_NAMESPACE"),
    ("{{path.olt.out_port}}", "TEMPLATE_NAMESPACE"),
    ("{{client.code | nope}}", "TEMPLATE_INVALID"),
    ("{{client.code}}" + "x" * 200, "TEMPLATE_TOO_LONG"),
])
def test_bad_templates_are_refused(bad, code):
    with pytest.raises(ValueError, match=code):
        check_match_template(bad)


def test_missing_or_empty_variable_matches_nothing():
    assert render_match_key(CSR, {}) is None
    assert render_match_key("{{client.code}}", {"client.code": "  "}) is None
    assert render_match_key(CSR, {"client.code": ""}) is None


def test_settings_carry_source_and_template():
    s = TrafficSettings(match_template=f"  {CSR} ", source="interface")
    assert (s.match_template, s.source) == (CSR, "interface")
    assert TrafficSettings(match_template="  ").match_template is None
    assert TrafficSettings().model_dump() == TRAFFIC_SETTINGS_DEFAULTS
    with pytest.raises(ValidationError):
        TrafficSettings(source="netflow")
    with pytest.raises(ValidationError, match="TEMPLATE_NAMESPACE"):
        TrafficSettings(match_template="{{capture.ppp_user}}")


def test_service_variables_feed_the_template():
    plan = SimpleNamespace(id=uuid.uuid4(), name="10M", download_mbps=10, upload_mbps=2,
                           provisioning_params=[{"key": "vlan", "value": 1203}])
    client = SimpleNamespace(id=uuid.uuid4(), name="Juan", code="CO0764", email=None,
                             phone=None, address=None, custom_fields=None)
    svc = SimpleNamespace(id=uuid.uuid4(), client=client, service_plan=plan, provisioning_params=None)
    variables, missing = service_variables(svc)
    assert missing == []
    assert render_match_key(CSR, variables) == "<pppoe-CO0764>"
    assert render_match_key("vlan{{service_plan.vlan}}", variables) == "vlan1203"
    assert render_match_key("{{service.id}}", variables) == str(svc.id)


def test_pppoe_username_is_no_longer_mapped():
    assert "pppoe_username" not in ClientService.__table__.c
    assert "pppoe_username" not in ClientServiceUpdate.model_fields
    assert "traffic_last_poll" in InventoryItem.__table__.c


def test_revision_chain():
    m = load("versions/tm1_traffic_match.py", "tm1_traffic_match")
    assert (m.revision, m.down_revision) == ("tm1_traffic_match", "sm1_snmp_traffic_metrics")


def test_tm2_revision_chain():
    m = load("versions/tm2_drop_pppoe_username.py", "tm2_drop_pppoe_username")
    assert (m.revision, m.down_revision) == ("tm2_drop_pppoe_username", "tm1_traffic_match")
