"""A built-in tool's typed evidence travels beside its result, never in it.

Documentation Search reports what its text delivered as a typed record
(``BuiltInToolResult.evidence``). The path to the agent is
``BuiltInToolResult → adapter → ToolResultEvent``; what is pinned:

- the adapter hands the record on as an ``InternalToolEvidence`` event — not
  an A2A event, so no part, no text, nothing a client or the model sees;
- the group puts it on the ``ToolResultEvent`` and keeps it out of the status
  stream, while the observation text and parts are exactly what they were;
- a retried call's record is the retry's; a tool without one yields none;
- ``parse_agent_events`` keeps its three-value shape.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

pytest.importorskip("a2a")

from app.tools.base import ToolResultEvent, ToolStatusEvent  # noqa: E402
from app.tools.builtin.adapter import BuiltInToolAdapter, InternalToolEvidence  # noqa: E402
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult  # noqa: E402
from app.tools.builtin.tool import BuiltInToolGroup  # noqa: E402
from app.utils.event_parser import parse_agent_events  # noqa: E402


class _Tool(BuiltInTool):
    name = "leaf"
    description = "reports evidence"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, evidence=None, text="the visible result") -> None:
        self._evidence = evidence
        self._text = text

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return BuiltInToolResult(content=[{"type": "text", "text": self._text}], evidence=self._evidence)


def _collect(agen):
    async def go():
        return [ev async for ev in agen]
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))


def test_the_adapter_hands_evidence_on_beside_the_a2a_events():
    record = object()
    adapter = BuiltInToolAdapter(tools=[_Tool(record)], llm=object(), name="docs")
    events = _collect(adapter.request(query="leaf", decided_calls=[{"name": "leaf", "arguments": {}}]))
    internal = [e for e in events if isinstance(e, InternalToolEvidence)]
    assert [(e.tool_name, e.evidence) for e in internal] == [("leaf", record)]
    # The A2A stream is what it was: the parser ignores the record and keeps its shape.
    text, usage, parts = parse_agent_events(events)
    assert text.endswith("the visible result") and "object at" not in text
    assert isinstance(usage, dict) and parts


def test_a_tool_without_evidence_adds_no_event():
    adapter = BuiltInToolAdapter(tools=[_Tool(None)], llm=object(), name="docs")
    events = _collect(adapter.request(query="leaf", decided_calls=[{"name": "leaf", "arguments": {}}]))
    assert not [e for e in events if isinstance(e, InternalToolEvidence)]


def test_the_group_puts_it_on_the_result_and_off_the_status_stream():
    record = {"typed": "record"}
    group = BuiltInToolGroup(config_name="docs", display_name="Docs", description="d",
                             functions=[_Tool(record)], llm=object())
    group.tool_id = "docs"
    events = _collect(group.execute_leaf(leaf_name="leaf", args={}, context_id="c", profile="p",
                                         arguments={}, variables={}))
    status = [e for e in events if isinstance(e, ToolStatusEvent)]
    assert status and not [e for e in status if isinstance(e.raw, InternalToolEvidence)]
    (result,) = [e for e in events if isinstance(e, ToolResultEvent)]
    assert result.evidence is record
    assert "typed" not in result.observation_text and result.observation_text.endswith("the visible result")
    # Same parts as without evidence.
    plain = BuiltInToolGroup(config_name="docs", display_name="Docs", description="d",
                             functions=[_Tool(None)], llm=object())
    plain.tool_id = "docs"
    (bare,) = [e for e in _collect(plain.execute_leaf(leaf_name="leaf", args={}, context_id="c", profile="p",
                                                      arguments={}, variables={}))
               if isinstance(e, ToolResultEvent)]
    assert bare.evidence is None
    assert [p.model_dump() for p in bare.observation_parts] == [p.model_dump() for p in result.observation_parts]


def test_a_retried_call_reports_the_retrys_evidence(monkeypatch, tmp_path):
    """The sandbox auto-recovery re-runs a call; its record replaces the first."""
    import app.tools.builtin.adapter as adapter_mod

    calls = []

    class _Denied(BuiltInTool):
        name = "leaf"
        description = "denied first"
        parameters: Dict[str, Any] = {"type": "object", "properties": {}}

        async def run(self, arguments):
            calls.append(1)
            if len(calls) == 1:
                return BuiltInToolResult(structured_content={"error": "Access denied", "message": "x"},
                                         evidence="first")
            return BuiltInToolResult(content=[{"type": "text", "text": "ok"}], evidence="second")

    target = tmp_path / "home" / "docs"
    target.mkdir(parents=True)
    monkeypatch.setattr(adapter_mod, "resolve_sandbox_recovery_dir", lambda *a, **k: str(target))

    async def _switch(*a, **k):
        return None

    import app.utils.working_directory as wd
    import app.events.runner as runner

    monkeypatch.setattr(wd, "switch_conversation_cwd", _switch)
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: None)
    adapter = BuiltInToolAdapter(tools=[_Denied()], llm=object(), name="docs")
    events = _collect(adapter.request(query="leaf", context_id="ctx",
                                      decided_calls=[{"name": "leaf", "arguments": {}}]))
    assert [e.evidence for e in events if isinstance(e, InternalToolEvidence)] == ["second"]
