"""Run triggers (doc 68 §3): the ProvisioningTrigger labels, the rtg1 revision,
the USER→MCP swap at run/job creation and DeviceActionLog.actor_kind."""
import inspect
import re

from _mi_helpers import load
from database_utils.models.isp import ProvisioningTrigger as T


def rtg1():
    return load("versions/rtg1_run_triggers.py", "rtg1_run_triggers")


def test_trigger_values():
    assert [t.value for t in T] == ["USER", "WORKFLOW", "API", "MCP", "BOT", "AUTO_SUSPENSION", "AUTO_REACTIVATION"]


def test_rtg1_revision_chain():  # down_revision is re-pointed at compose, with this assertion
    m = rtg1()
    assert m.revision == "rtg1_run_triggers" and m.down_revision == "et1_edge_telnet"


def test_rtg1_labels_match_the_python_enum():
    assert {T(v) for v in rtg1().NEW_LABELS} == {T.MCP, T.BOT, T.AUTO_SUSPENSION, T.AUTO_REACTIVATION}


def test_rtg1_rewrites_no_row():
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", inspect.getsource(rtg1().upgrade), re.I)
