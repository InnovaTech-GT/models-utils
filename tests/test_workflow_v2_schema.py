"""wf2 schema changes (plan 64a §2.3-2.4): EdgeDef.branch takes any node-key
shaped name, NodeDef.locked is omitted from dumps when false (a phase 1
definition dumps unchanged), and the turn/reply schemas."""
import uuid

import pytest
from pydantic import ValidationError

from database_utils.schemas.workflow_v2 import (
    EdgeDef,
    NodeDef,
    RunReplyIn,
    RunTurnOut,
    WorkflowDefinition,
    WorkflowRunDetail,
)

DEF = {"schema": 1, "trigger": {"type": "bot", "key": "chat", "config": {}}, "start": "ask",
       "nodes": [{"key": "ask", "action": "messaging.ask", "config": {"text": "hi"}},
                 {"key": "end", "action": "flow.end"}],
       "edges": [{"from": "ask", "to": "end", "branch": "timeout"}]}


@pytest.mark.parametrize("branch", ["yes", "no", "si", "answered", "invalid", "timeout", "verified", "opt_10"])
def test_edge_branch_accepts_option_ids(branch):
    assert EdgeDef.model_validate({"from": "a", "to": "b", "branch": branch}).branch == branch


@pytest.mark.parametrize("branch", ["", "Yes", "1a", "a-b", "a b", "x" * 33])
def test_edge_branch_refuses_bad_names(branch):
    with pytest.raises(ValidationError):
        EdgeDef.model_validate({"from": "a", "to": "b", "branch": branch})


def test_locked_is_omitted_when_false():
    d = WorkflowDefinition.model_validate(DEF)
    dumped = d.model_dump(by_alias=True, mode="json")
    assert all("locked" not in n for n in dumped["nodes"])
    # Round trip of a phase 1 shaped definition is stable.
    assert WorkflowDefinition.model_validate(dumped).model_dump(by_alias=True, mode="json") == dumped
    # Other defaults are still filled.
    assert dumped["nodes"][1]["config"] == {} and dumped["edges"][0]["on"] == "completed"


def test_locked_true_is_dumped():
    n = NodeDef.model_validate({"key": "who", "action": "chat.verify_identity", "locked": True})
    assert n.model_dump()["locked"] is True
    assert '"locked":true' in n.model_dump_json()
    with pytest.raises(ValidationError):
        NodeDef.model_validate({"key": "who", "action": "x", "locked": "maybe"})


def test_reply_needs_exactly_one():
    assert RunReplyIn(text="hola").option_id is None
    assert RunReplyIn(option_id="si").text is None
    composite = "k" * 32 + "." + "o" * 32  # rendered "{node_key}.{option_id}"
    assert RunReplyIn(option_id=composite).option_id == composite
    for bad in ({}, {"text": "a", "option_id": "si"}, {"text": ""}, {"text": "x" * 1001},
                {"option_id": "x" * 66}, {"text": "a", "extra": 1}):
        with pytest.raises(ValidationError):
            RunReplyIn.model_validate(bad)


def test_turn_and_detail_defaults():
    assert RunTurnOut(handled=False).model_dump()["messages"] == []
    t = RunTurnOut.model_validate({
        "run_id": str(uuid.uuid4()), "status": "WAITING", "awaiting": True, "wait_seconds": 1800, "seq": 2,
        "messages": [{"type": "options", "text": "¿Pagar?", "button": "Ver opciones",
                      "options": [{"id": "si", "title": "Sí"}]}],
        "hand_off": {"receipt_link_id": "abc"}})
    assert t.handled and t.messages[0].options[0].description is None and t.hand_off.flow is None
    with pytest.raises(ValidationError):
        RunTurnOut.model_validate({"hand_off": {"flow": "elsewhere"}})
    d = WorkflowRunDetail(id=uuid.uuid4(), status="COMPLETED")
    assert d.conversation == [] and d.awaiting is False
