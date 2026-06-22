"""Tests for the System-File-style subtool routing in BuiltInToolAdapter.

The reasoning agent prefixes its free-text Action_Input with the intended
subtool name (e.g. ``overwrite_file path="..." diff="..."``). The adapter's
child dispatcher used to re-pick the function with a high-temperature "auto"
choice and sometimes ran the wrong tool (e.g. read_file when overwrite_file was
asked). These tests pin the deterministic routing: ``_leading_tool_name``
detects the named subtool, and ``request()`` forces ``tool_choice`` to it at
``temperature=0``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.constants import ChatCompletionTypeEnum
from app.tools.builtin.adapter import (
    BuiltInToolAdapter,
    _leading_tool_name,
    _looks_like_diff,
)
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult


_NAMES = [
    "search_files", "grep_files", "list_files", "get_file_info", "read_file",
    "write_file", "overwrite_file", "register_file_watcher",
]


# --- _leading_tool_name unit tests -----------------------------------------

def test_leading_name_basic() -> None:
    assert _leading_tool_name('overwrite_file path="x" diff="..."', _NAMES) == "overwrite_file"
    assert _leading_tool_name('read_file path="x"', _NAMES) == "read_file"
    assert _leading_tool_name('grep_files pattern="y"', _NAMES) == "grep_files"


def test_leading_name_with_thought_hint() -> None:
    # The reasoning agent appends a "(How would you think ...)" hint after args.
    q = 'overwrite_file path="x" diff="d" (How would you think about this: ...)'
    assert _leading_tool_name(q, _NAMES) == "overwrite_file"


def test_leading_name_leading_whitespace() -> None:
    assert _leading_tool_name('   read_file path="x"', _NAMES) == "read_file"


def test_leading_name_boundary_variants() -> None:
    assert _leading_tool_name("read_file:", _NAMES) == "read_file"
    assert _leading_tool_name("grep_files(", _NAMES) == "grep_files"
    assert _leading_tool_name("write_file", _NAMES) == "write_file"  # bare name


def test_leading_name_natural_language_is_none() -> None:
    assert _leading_tool_name("Read the contents of conversation.txt", _NAMES) is None
    # 'search files' has a space, not the 'search_files' token.
    assert _leading_tool_name("search files for X", _NAMES) is None
    assert _leading_tool_name("please overwrite the file", _NAMES) is None


def test_leading_name_not_in_list_is_none() -> None:
    # e.g. register_file_watcher suppressed on event runs -> not forced.
    names_without_watcher = [n for n in _NAMES if n != "register_file_watcher"]
    assert _leading_tool_name("register_file_watcher path=x", names_without_watcher) is None


def test_leading_name_prefix_collision_picks_longest() -> None:
    # A name that is a prefix of another must not shadow the longer match.
    names = ["read", "read_file"]
    assert _leading_tool_name('read_file path="x"', names) == "read_file"


# --- _looks_like_diff unit tests -------------------------------------------

def test_looks_like_diff() -> None:
    assert _looks_like_diff("@@ -2,7 +2,7 @@\n-a\n+b") is True
    assert _looks_like_diff("   @@ -1 +1 @@\n-x\n+y") is True  # leading whitespace
    assert _looks_like_diff('read_file path="x"') is False
    assert _looks_like_diff("- buy milk\n- buy eggs") is False  # not a hunk header
    assert _looks_like_diff("") is False


# --- request()-level test: forced tool_choice + temperature=0 --------------

class _FakeTool(BuiltInTool):
    name = "overwrite_file"
    description = "fake overwrite"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return BuiltInToolResult(structured_content={"text": "ok"})


class _FakeReadTool(BuiltInTool):
    name = "read_file"
    description = "fake read"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return BuiltInToolResult(structured_content={"text": "ok"})


class _RecordingLLM:
    """Minimal LLMProvider stand-in that records the routing call kwargs."""
    provider_name = "fake"
    model_label = "fake-model"
    default_reasoning_effort = None

    def __init__(self) -> None:
        self.recorded: Dict[str, Any] = {}

    async def chat_completion(self, *, messages=None, tools=None,
                              tool_choice=None, temperature=None, **kwargs):
        self.recorded = {
            "messages": messages, "tools": tools,
            "tool_choice": tool_choice, "temperature": temperature,
        }
        yield {
            "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
            "data": {"function": [{"name": "overwrite_file",
                                   "arguments": {"path": "x", "diff": "d"}}]},
        }
        yield {"type": ChatCompletionTypeEnum.DONE,
               "input_tokens": 0, "output_tokens": 0}


def _drive(adapter: BuiltInToolAdapter, query: str) -> None:
    async def _consume() -> None:
        async for _ in adapter.request(query=query):
            pass
    asyncio.run(_consume())


def test_request_forces_named_subtool_at_temp_zero() -> None:
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(tools=[_FakeTool()], llm=llm, name="system_file")
    _drive(adapter, 'overwrite_file path="conversation.txt" diff="@@ -1 +1 @@\n-a\n+b"')
    assert llm.recorded["tool_choice"] == {
        "type": "function", "function": {"name": "overwrite_file"}}
    assert llm.recorded["temperature"] == 0


def test_request_auto_when_no_named_subtool() -> None:
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(tools=[_FakeTool()], llm=llm, name="system_file")
    _drive(adapter, "please change the file somehow")
    assert llm.recorded["tool_choice"] == "auto"
    assert llm.recorded["temperature"] == 0


def test_request_bare_diff_forces_overwrite_file() -> None:
    # A bare diff (no leading subtool name) must route to overwrite_file, not
    # fall back to "auto" (where the router historically picked grep_files).
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(tools=[_FakeTool()], llm=llm, name="system_file")
    _drive(adapter, "@@ -2,7 +2,7 @@\n-James: hi\n+Steve: hi")
    assert llm.recorded["tool_choice"] == {
        "type": "function", "function": {"name": "overwrite_file"}}


def test_request_forcing_sends_only_the_forced_tool() -> None:
    # A forced subtool must send ONLY that tool's schema — providers that
    # validate schemas (Groq) otherwise choke on sibling schemas, and Groq
    # rejects disable_tool_validation alongside a named tool_choice.
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(tools=[_FakeTool(), _FakeReadTool()], llm=llm, name="system_file")
    _drive(adapter, 'overwrite_file path="x" diff="d"')
    sent = [t["function"]["name"] for t in (llm.recorded["tools"] or [])]
    assert sent == ["overwrite_file"]
    assert llm.recorded["tool_choice"] == {
        "type": "function", "function": {"name": "overwrite_file"}}


def test_request_auto_sends_all_tools() -> None:
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(tools=[_FakeTool(), _FakeReadTool()], llm=llm, name="system_file")
    _drive(adapter, "please change the file somehow")
    sent = sorted(t["function"]["name"] for t in (llm.recorded["tools"] or []))
    assert sent == ["overwrite_file", "read_file"]
    assert llm.recorded["tool_choice"] == "auto"


def test_request_injects_group_instructions_into_system_prompt() -> None:
    llm = _RecordingLLM()
    adapter = BuiltInToolAdapter(
        tools=[_FakeTool()], llm=llm, name="system_file",
        tool_instructions="STEER: edit with overwrite_file.",
    )
    _drive(adapter, "please change the file somehow")
    system_msg = llm.recorded["messages"][0]["content"]
    assert "STEER: edit with overwrite_file." in system_msg
