"""Unit tests for the ``claude_code`` built-in tool.

Exercises the SDK bridge with a fake ``claude_agent_sdk`` injected into
``sys.modules`` (no real SDK / CLI / network), plus the missing-SDK gate,
concurrency limits, the activity translator, and the feature/guidance wiring.

Tests drive coroutines with ``asyncio.run`` (matching the repo's other tool
tests — no pytest-asyncio needed). The module-level task registry is cleared
around every test by an autouse fixture.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest


# ── fake SDK message / block types (dispatched by class name) ─────────────────
@dataclass
class SystemMessage:
    subtype: str
    data: Optional[dict] = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-test"


@dataclass
class UserMessage:
    content: Any


@dataclass
class ResultMessage:
    subtype: str = "success"
    result: str = ""
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "sess-1"
    total_cost_usd: float = 0.01
    duration_ms: int = 1234
    usage: Optional[dict] = None


@dataclass
class ThinkingBlock:
    thinking: str


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = ""
    is_error: bool = False


def install_fake_sdk(monkeypatch, produce, *, aenter_error: Optional[Exception] = None):
    """Install a fake ``claude_agent_sdk`` whose client streams ``produce(client)``."""
    mod = types.ModuleType("claude_agent_sdk")

    @dataclass
    class ClaudeAgentOptions:
        cwd: Any = None
        permission_mode: Any = None
        model: Any = None
        resume: Any = None
        max_turns: Any = None
        max_budget_usd: Any = None
        allowed_tools: Any = None
        disallowed_tools: Any = None
        cli_path: Any = None
        env: Any = None
        system_prompt: Any = None
        setting_sources: Any = None
        include_partial_messages: bool = False
        stderr: Any = None

    class CLINotFoundError(Exception):
        pass

    class ProcessError(Exception):
        def __init__(self, message: str = "", exit_code: Optional[int] = None):
            super().__init__(message)
            self.exit_code = exit_code

    class ClaudeSDKClient:
        def __init__(self, options=None):
            self.options = options
            self.gate = asyncio.Event()
            self.interrupted = False

        async def __aenter__(self):
            if aenter_error is not None:
                raise aenter_error
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            self.prompt = prompt

        async def interrupt(self):
            self.interrupted = True
            self.gate.set()

        def receive_response(self):
            return produce(self)

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.CLINotFoundError = CLINotFoundError
    mod.ProcessError = ProcessError
    mod.ClaudeSDKClient = ClaudeSDKClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """Isolate the module-level registry and keep the activity feed hermetic.

    The runner tests care about task lifecycle + the payloads Cremind sees, not
    the live UI stream (that path — SDK message → step — is covered by the
    translator tests, and the wire/store side is TypeScript). Each test uses its
    own ``asyncio.run`` loop, so we neutralise the real streaming singleton and
    its coalescing timers here (a module-singleton bus + debounce tasks span
    loops badly under back-to-back ``asyncio.run`` calls), and resolve the
    conversation id without touching the DB. Step *tracking* stays real so the
    wait-heartbeat's ``total_steps`` count is exercised.
    """
    from app.tools.builtin import claude_code_runner as r
    import app.agent.agent_activity as aa

    r._task_registry.clear()

    async def _resolve(profile, context_id):
        return context_id

    async def _noop(self):
        return None

    monkeypatch.setattr(aa, "_resolve_conversation_id", _resolve)
    monkeypatch.setattr(aa.AgentActivity, "_publish_now", _noop)
    monkeypatch.setattr(aa.AgentActivity, "_patch_persisted", _noop)
    monkeypatch.setattr(aa.AgentActivity, "_schedule_flush", lambda self: None)
    yield
    r._task_registry.clear()


def _run_tool(**args):
    from app.tools.builtin.claude_code import ClaudeCodeRunTool

    return ClaudeCodeRunTool().run(args)


def _status_tool(**args):
    from app.tools.builtin.claude_code import ClaudeCodeStatusTool

    return ClaudeCodeStatusTool().run(args)


# ── missing SDK ───────────────────────────────────────────────────────────────
def test_missing_sdk_returns_missing_dependency(monkeypatch, tmp_path):
    # Setting the module to None makes `import claude_agent_sdk` raise ImportError.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    res = asyncio.run(
        _run_tool(prompt="do a thing", working_directory=str(tmp_path), _variables={})
    )
    sc = res.structured_content
    assert sc["error"] == "MissingDependency"
    assert sc["feature_key"] == "claude_code"
    assert "claude-code" in sc["extras"]


# ── fast task completes within the grace window ───────────────────────────────
def test_fast_task_completes_in_grace_window(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "sess-42"})
        yield AssistantMessage(content=[
            ThinkingBlock(thinking="Let me look."),
            ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"}),
        ])
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="file body")])
        yield ResultMessage(
            subtype="success", result="Done. Created a.py", is_error=False,
            num_turns=3, session_id="sess-42", total_cost_usd=0.1234,
            duration_ms=5000,
            usage={"input_tokens": 10, "output_tokens": 20,
                   "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2},
        )

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="create a.py", working_directory=str(tmp_path),
                  _context_id="conv-1", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["result"] == "Done. Created a.py"
    assert sc["session_id"] == "sess-42"
    assert sc["num_turns"] == 3
    assert sc["total_cost_usd"] == 0.1234
    # Token usage folded onto the BuiltInToolResult (usage record source).
    assert res.token_usage == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2,
    }


# ── slow task: handle → wait → final; usage folded exactly once ───────────────
def test_slow_task_handle_then_wait_final_usage_once(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r
    from app.tools.builtin.claude_code import ClaudeCodeWaitTool

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "sess-slow"})
        yield AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "make"})])
        await client.gate.wait()  # block until the test releases it
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="built")])
        yield ResultMessage(
            subtype="success", result="Built the project", session_id="sess-slow",
            num_turns=7, total_cost_usd=0.5, usage={"input_tokens": 1, "output_tokens": 2},
        )

    install_fake_sdk(monkeypatch, produce)

    async def body():
        run_res = await _run_tool(
            prompt="build it", working_directory=str(tmp_path),
            _context_id="conv-2", _profile="default", _variables={},
        )
        assert run_res.structured_content["status"] == "running"
        task_id = run_res.structured_content["task_id"]

        task = r.get_task(task_id)
        assert task is not None and task.client is not None
        task.client.gate.set()  # release the blocked stream

        wait_res = await ClaudeCodeWaitTool().run({"task_id": task_id, "timeout": 5})
        assert wait_res.structured_content["status"] == "completed"
        assert wait_res.structured_content["result"] == "Built the project"
        # Usage folded on the wait that first observed completion...
        assert wait_res.token_usage == {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        # ...and NOT again on a re-issued wait.
        again = await ClaudeCodeWaitTool().run({"task_id": task_id, "timeout": 5})
        assert again.token_usage is None

    asyncio.run(body())


# ── stop interrupts a hung task; session stays resumable ──────────────────────
def test_stop_interrupts_resumable(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "sess-hang"})
        # Hang forever; interrupt() in this fake does NOT complete the stream,
        # forcing stop_task's cancel fallback.
        never = asyncio.Event()
        await never.wait()
        yield ResultMessage()  # unreachable

    # interrupt() must not set the gate/complete for this test → override.
    mod = install_fake_sdk(monkeypatch, produce)

    async def _noop_interrupt(self):
        self.interrupted = True

    mod.ClaudeSDKClient.interrupt = _noop_interrupt

    async def body():
        run_res = await _run_tool(
            prompt="loop", working_directory=str(tmp_path),
            _context_id="conv-3", _profile="default", _variables={},
        )
        task_id = run_res.structured_content["task_id"]
        task = r.get_task(task_id)
        status = await r.stop_task(task, grace=0.1)
        assert status == "interrupted"
        assert task.result["status"] == "interrupted"
        assert task.result["session_id"] == "sess-hang"

    asyncio.run(body())


# ── one task per conversation ─────────────────────────────────────────────────
def test_one_task_per_conversation(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "s"})
        await client.gate.wait()
        yield ResultMessage(result="ok")

    install_fake_sdk(monkeypatch, produce)

    async def body():
        first = await _run_tool(
            prompt="one", working_directory=str(tmp_path),
            _context_id="conv-4", _profile="default", _variables={},
        )
        assert first.structured_content["status"] == "running"
        second = await _run_tool(
            prompt="two", working_directory=str(tmp_path),
            _context_id="conv-4", _profile="default", _variables={},
        )
        assert second.structured_content["error"] == "TaskAlreadyRunning"
        assert second.structured_content["task_id"] == first.structured_content["task_id"]

    asyncio.run(body())


# ── wait on a bogus id ────────────────────────────────────────────────────────
def test_wait_unknown_task_id(monkeypatch):
    from app.tools.builtin.claude_code import ClaudeCodeWaitTool

    res = asyncio.run(ClaudeCodeWaitTool().run({"task_id": "deadbeef"}))
    sc = res.structured_content
    assert sc["error"] == "TaskNotFound"
    assert "known_task_ids" in sc


# ── activity translator (pure; no SDK) ────────────────────────────────────────
def test_tool_use_label_mapping():
    from app.tools.builtin.claude_code_activity import tool_use_label

    assert tool_use_label("Bash", {"command": "ls -la"})[0] == "$ ls -la"
    assert tool_use_label("Edit", {"file_path": "x.py"})[0] == "Editing x.py"
    assert tool_use_label("Read", {"file_path": "y.py"})[0] == "Reading y.py"
    assert tool_use_label("Grep", {"pattern": "TODO"})[0] == "Searching TODO"
    assert tool_use_label("Task", {"description": "sub"})[0] == "Sub-agent: sub"
    assert tool_use_label("SomeMcpTool", {"a": 1})[0] == "Tool: SomeMcpTool"


def test_apply_sdk_message_builds_steps(monkeypatch):
    from app.tools.builtin.claude_code_activity import apply_sdk_message

    class FakeActivity:
        def __init__(self):
            self.added = []
            self.resolved = []

        async def add_step(self, *, kind, label, detail=None, step_id=None, status=None):
            self.added.append((kind, label, step_id, status))
            return step_id or f"s{len(self.added)}"

        async def resolve_step(self, step_id, *, status, detail_suffix=None):
            self.resolved.append((step_id, status))

    async def body():
        act = FakeActivity()
        await apply_sdk_message(act, AssistantMessage(content=[
            ThinkingBlock(thinking="hmm\nmore"),
            TextBlock(text="hello"),
            ToolUseBlock(id="t1", name="Write", input={"file_path": "z.py", "content": "x"}),
        ]))
        await apply_sdk_message(act, UserMessage(content=[
            ToolResultBlock(tool_use_id="t1", content="wrote", is_error=False),
        ]))
        kinds = [a[0] for a in act.added]
        assert kinds == ["thinking", "text", "tool_use"]
        # tool_use carried the block id + running status
        assert ("tool_use", "Writing z.py", "t1", "running") in act.added
        assert act.resolved == [("t1", "ok")]

    asyncio.run(body())


# ── feature + guidance wiring ─────────────────────────────────────────────────
def test_feature_and_pip_spec():
    from app.features.manifest import FEATURES, pip_spec

    feat = FEATURES["claude_code"]
    assert feat.extras == ("claude-code",)
    assert feat.probes == ("claude_agent_sdk",)
    assert feat.requires_restart is False
    assert pip_spec(["claude_code"]).startswith("cremind[claude-code]==")


def test_guidance_present_only_when_enabled():
    from app.agent.reasoning_agent import _build_claude_code_guidance

    assert _build_claude_code_guidance([]) == ""

    class Group:
        config_name = "claude_code"
        tool_id = "claude_code"

    text = _build_claude_code_guidance([Group()])
    assert "claude_code__run" in text
    assert "claude_code__wait" in text
    assert "claude_code__stop" in text
    assert "claude_code__status" in text


# ── auth-failure classification (the not-logged-in case) ──────────────────────
def test_result_message_auth_error_is_classified(monkeypatch, tmp_path):
    """A not-logged-in run surfaces as an is_error ResultMessage — it must be
    labelled AuthenticationError with remediation, not a generic error."""
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "s"})
        yield ResultMessage(
            subtype="error_during_execution",
            result="Could not resolve authentication method. Expected one of apiKey, "
            "authToken, credentials, config, or profile to be set.",
            is_error=True,
        )

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "AuthenticationError"
    assert "claude login" in sc["remediation"].lower()


def test_result_message_generic_error_stays_generic(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "s"})
        yield ResultMessage(
            subtype="error_max_turns",
            result="Hit the maximum number of turns.",
            is_error=True,
        )

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "ClaudeCodeError"


def test_raised_auth_exception_is_classified(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(client):
        if False:
            yield  # make it an async generator
        raise RuntimeError("HTTP 401 Unauthorized: invalid x-api-key")

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    assert res.structured_content["error"] == "AuthenticationError"


# ── status leaf (answers "is Claude logged in?") ──────────────────────────────
def test_status_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["available"] is False
    assert sc["sdk_installed"] is False


def test_status_no_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    async def produce(client):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["available"] is True
    assert sc["credentials_configured"] is False
    assert sc["credential_source"] is None
    assert "probe=true" in sc["message"]


def test_status_with_tool_variable_key(monkeypatch):
    async def produce(client):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _status_tool(_profile="default", _variables={"CLAUDE_CODE_API_KEY": "sk-test"})
    )
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "tool_variable_api_key"


def test_status_probe_authenticated(monkeypatch, tmp_path):
    async def produce(client):
        yield ResultMessage(subtype="success", result="OK", is_error=False)

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={"CLAUDE_CODE_API_KEY": "sk-x"})
    )
    sc = res.structured_content
    assert sc["logged_in"] is True
    assert "ready" in sc["message"].lower()


def test_status_probe_not_authenticated(monkeypatch, tmp_path):
    async def produce(client):
        yield ResultMessage(
            subtype="error_during_execution",
            result="Could not resolve authentication method.",
            is_error=True,
        )

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["logged_in"] is False
    assert "not authenticated" in sc["message"].lower()
