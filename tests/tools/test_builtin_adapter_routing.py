"""BuiltInToolAdapter / BuiltInToolGroup as pure executors (native function calling).

The inner routing LLM was removed: the reasoning model picks the sub-tool and
its typed args, and the adapter executes the *decided* call directly. These tests
pin that ``build_specs()`` exposes each sub-tool's real JSON-Schema and that
``request(decided_calls=...)`` / ``execute_leaf(...)`` run the chosen tool with
no LLM round.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.tools.builtin.adapter import BuiltInToolAdapter
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.tool import BuiltInToolGroup
from app.tools.base import ToolResultEvent
from app.utils.event_parser import parse_agent_events


class _FakeTool(BuiltInTool):
    name = "overwrite_file"
    description = "fake overwrite"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "diff": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        self.calls.append(arguments)
        return BuiltInToolResult(structured_content={"text": "ok"})


def _collect(agen) -> list:
    out: list = []

    async def _consume() -> None:
        async for ev in agen:
            out.append(ev)

    asyncio.run(_consume())
    return out


# --- build_specs exposes the real JSON-Schema -------------------------------

def test_build_specs_exposes_full_schema() -> None:
    adapter = BuiltInToolAdapter(tools=[_FakeTool()], llm=object(), name="system_file")
    specs = adapter.build_specs()
    assert specs[0]["function"]["name"] == "overwrite_file"
    params = specs[0]["function"]["parameters"]
    assert params["properties"]["path"]["type"] == "string"
    assert params["required"] == ["path"]


# --- request(decided_calls=) executes directly, no LLM ----------------------

def test_request_executes_decided_call_without_llm() -> None:
    tool = _FakeTool()
    adapter = BuiltInToolAdapter(tools=[tool], llm=object(), name="system_file")
    events = _collect(adapter.request(
        query="overwrite_file",
        decided_calls=[{"name": "overwrite_file", "arguments": {"path": "x", "diff": "d"}}],
    ))
    assert tool.calls and tool.calls[0]["path"] == "x"
    obs_text, _usage, _parts = parse_agent_events(events)
    assert "ok" in obs_text


# --- BuiltInToolGroup namespaces leaves and dispatches the chosen one -------

def _group(*tools) -> BuiltInToolGroup:
    g = BuiltInToolGroup(
        config_name="system_file", display_name="System File",
        description="File assistant.", functions=list(tools), llm=object(),
    )
    g.tool_id = "system_file"
    return g


def test_group_leaf_specs_are_namespaced() -> None:
    group = _group(_FakeTool())
    specs = group.leaf_function_specs(context_id="c", profile="p")
    assert specs[0].name == "system_file__overwrite_file"
    assert specs[0].leaf_name == "overwrite_file"
    # Full schema is preserved under the namespaced function name.
    assert specs[0].schema["function"]["name"] == "system_file__overwrite_file"
    assert specs[0].schema["function"]["parameters"]["required"] == ["path"]


def test_group_execute_leaf_runs_tool_and_returns_result() -> None:
    tool = _FakeTool()
    group = _group(tool)
    events = _collect(group.execute_leaf(
        leaf_name="overwrite_file", args={"path": "x", "diff": "d"},
        context_id="c", profile="p", arguments={}, variables={},
    ))
    assert tool.calls and tool.calls[0]["path"] == "x"
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results and "ok" in results[-1].observation_text


# --- Tool Variables never reach a log sink ----------------------------------

def test_the_execute_log_names_variables_without_their_values() -> None:
    """Tool Variables are resolved WITH secrets, so their values must not be logged.

    The adapter hands a tool its variables in cleartext - that is the point of
    them - and then logged the whole argument dict at INFO. That line goes to
    ``logs/app.log`` and, through the unconditional DEBUG bus sink, to the
    Developer log stream in the browser, so an API key or a pasted sign-in token
    was readable by anyone with either. The names are what makes the line useful
    when a tool runs unconfigured; the values never were.
    """
    from app.utils.logger import logger as loguru_logger

    records: list[str] = []
    sink_id = loguru_logger.add(lambda message: records.append(str(message)), level="INFO")
    try:
        tool = _FakeTool()
        adapter = BuiltInToolAdapter(tools=[tool], llm=object(), name="system_file")
        _collect(adapter.request(
            query="overwrite_file",
            decided_calls=[{"name": "overwrite_file", "arguments": {"path": "x"}}],
            metadata={"variables": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-supersecret"}},
        ))
    finally:
        loguru_logger.remove(sink_id)

    logged = "\n".join(records)
    assert "sk-ant-oat01-supersecret" not in logged
    # The tool itself still gets the real value - redaction is for the log only.
    assert tool.calls[0]["_variables"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-supersecret"
    # And the line still says which variables were in play.
    assert "CLAUDE_CODE_OAUTH_TOKEN" in logged
