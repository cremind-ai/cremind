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
    usage: Optional[dict] = None
    parent_tool_use_id: Optional[str] = None


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
    model_usage: Optional[dict] = None


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
def _clean_registry(monkeypatch, tmp_path):
    """Isolate the module-level registry and keep the activity feed hermetic.

    The runner tests care about task lifecycle + the payloads Cremind sees, not
    the live UI stream (that path — SDK message → step — is covered by the
    translator tests, and the wire/store side is TypeScript). Each test uses its
    own ``asyncio.run`` loop, so we neutralise the real streaming singleton and
    its coalescing timers here (a module-singleton bus + debounce tasks span
    loops badly under back-to-back ``asyncio.run`` calls), and resolve the
    conversation id without touching the DB. Step *tracking* stays real so the
    wait-heartbeat's ``total_steps`` count is exercised.

    Credentials are isolated by *environment*, not by patching private module
    constants: ``CLAUDE_CONFIG_DIR`` + ``CREMIND_SYSTEM_DIR`` are what
    ``app.config.coding_cli_homes`` reads, so pointing them at ``tmp_path``
    keeps every credential question off the developer's own ``~/.claude`` and
    lets a test create a login simply by writing the file. ``is_container`` is
    pinned False so the container-only legacy ``~`` tier can never fire either.
    ``list_models`` is stubbed to an empty success so ``status`` never hits the
    network; tests that care about the model list re-stub it locally.
    """
    from app.config import runtime_env
    from app.config.settings import BaseConfig
    from app.tools.builtin import claude_code_runner as r
    import app.agent.agent_activity as aa

    r._task_registry.clear()
    r._models_cache.clear()

    async def _resolve(profile, context_id):
        return context_id

    async def _noop(self):
        return None

    async def _empty_models(variables, profile, *, force_refresh=False):
        return {"models": [], "source": None, "cached": False}

    monkeypatch.setattr(aa, "_resolve_conversation_id", _resolve)
    monkeypatch.setattr(aa.AgentActivity, "_publish_now", _noop)
    monkeypatch.setattr(aa.AgentActivity, "_patch_persisted", _noop)
    monkeypatch.setattr(aa.AgentActivity, "_schedule_flush", lambda self: None)
    monkeypatch.setattr(r, "list_models", _empty_models)

    system_dir = tmp_path / "system"
    shared = tmp_path / "shared-claude"
    system_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(runtime_env, "is_container", lambda *a, **k: False)

    yield types.SimpleNamespace(system_dir=system_dir, shared=shared)
    r._task_registry.clear()
    r._models_cache.clear()


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
        yield AssistantMessage(
            content=[
                ThinkingBlock(thinking="Let me look."),
                ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"}),
            ],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 50,
                   "cache_read_input_tokens": 120000, "cache_creation_input_tokens": 400},
        )
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
    # claude_code is a delegated sub-agent: its usage is NOT folded into the turn's
    # Cremind accounting (shown only in the Agent Activity panel). The model still
    # sees the four-way split via structured_content["usage"].
    assert res.token_usage is None
    assert sc["usage"] == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2,
    }
    # Live context usage from the AssistantMessage reached the activity snapshot:
    # prompt tokens (1000 + 120000 + 400), window resolved from the model id.
    task = r.get_task(sc["task_id"])
    assert task is not None
    usage = task.activity.snapshot()["usage"]
    assert usage == {"context_tokens": 121400, "context_window": 1000000}


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
        # Delegated sub-agent usage is never folded into the turn; the model-visible
        # payload still carries the split.
        assert wait_res.token_usage is None
        assert wait_res.structured_content["usage"] == {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
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


class _UsageActivity:
    """FakeActivity that records update_usage calls (and swallows steps)."""

    def __init__(self):
        self.usage = None
        self.usage_calls = []

    async def add_step(self, *, kind, label, detail=None, step_id=None, status=None):
        return step_id or "s1"

    async def resolve_step(self, step_id, *, status, detail_suffix=None):
        return None

    async def update_usage(self, usage):
        self.usage = usage
        self.usage_calls.append(usage)


def test_apply_sdk_message_updates_context_usage(monkeypatch):
    from app.tools.builtin.claude_code_activity import apply_sdk_message

    async def body():
        act = _UsageActivity()
        await apply_sdk_message(act, AssistantMessage(
            content=[TextBlock(text="hi")],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 12, "cache_read_input_tokens": 100_000,
                   "cache_creation_input_tokens": 400, "output_tokens": 50},
        ))
        # Prompt tokens only (output excluded); window resolved from the model id.
        assert act.usage == {"context_tokens": 100_412, "context_window": 1_000_000}

    asyncio.run(body())


def test_context_window_resolution_fallbacks(monkeypatch):
    from app.tools.builtin.claude_code_activity import apply_sdk_message

    async def body():
        # Dated model id → stripped to the alias id that the catalog keys.
        dated = _UsageActivity()
        await apply_sdk_message(dated, AssistantMessage(
            content=[], model="claude-sonnet-4-6-20260203",
            usage={"input_tokens": 5, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 1},
        ))
        assert dated.usage == {"context_tokens": 5, "context_window": 1_000_000}

        # Unknown model → tokens only, no window (UI drops the percentage).
        unknown = _UsageActivity()
        await apply_sdk_message(unknown, AssistantMessage(
            content=[], model="claude-mystery-9",
            usage={"input_tokens": 7, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 1},
        ))
        assert unknown.usage == {"context_tokens": 7, "context_window": None}

    asyncio.run(body())


def test_subagent_usage_excluded(monkeypatch):
    from app.tools.builtin.claude_code_activity import apply_sdk_message

    async def body():
        act = _UsageActivity()
        # Task-tool subagent messages carry parent_tool_use_id — their context is
        # separate and must not overwrite the main loop's indicator.
        await apply_sdk_message(act, AssistantMessage(
            content=[TextBlock(text="sub")],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 999, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 1},
            parent_tool_use_id="tu_1",
        ))
        assert act.usage is None
        assert act.usage_calls == []

    asyncio.run(body())


def test_result_message_usage_fallback(monkeypatch):
    from app.tools.builtin.claude_code_activity import apply_sdk_message

    async def body():
        # No prior per-message usage → ResultMessage.usage is folded in, with the
        # window taken from a single-model model_usage.
        cold = _UsageActivity()
        await apply_sdk_message(cold, ResultMessage(
            usage={"input_tokens": 30, "cache_read_input_tokens": 10,
                   "cache_creation_input_tokens": 0, "output_tokens": 5},
            model_usage={"claude-sonnet-4-6": {"input_tokens": 30}},
        ))
        assert cold.usage == {"context_tokens": 40, "context_window": 1_000_000}

        # Prior usage already set → ResultMessage does NOT overwrite it.
        warm = _UsageActivity()
        warm.usage = {"context_tokens": 111, "context_window": 200_000}
        await apply_sdk_message(warm, ResultMessage(
            usage={"input_tokens": 30, "cache_read_input_tokens": 10,
                   "cache_creation_input_tokens": 0, "output_tokens": 5},
        ))
        assert warm.usage == {"context_tokens": 111, "context_window": 200_000}
        assert warm.usage_calls == []

    asyncio.run(body())


# ── permission-mode awareness in the result payloads ─────────────────────────
def _quick_result(monkeypatch, **result_kwargs):
    """Install a fake SDK that finishes immediately with one ResultMessage."""
    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "sess-pm"})
        yield ResultMessage(**result_kwargs)

    install_fake_sdk(monkeypatch, produce)


def test_plan_mode_completed_run_carries_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    _quick_result(monkeypatch, result="Here is a plan.", num_turns=2, session_id="sess-pm")

    res = asyncio.run(_run_tool(
        prompt="optimize this", working_directory=str(tmp_path),
        _context_id="conv-pm-1", _profile="default",
        _variables={r.Var.PERMISSION_MODE: "plan"},
    ))
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["effective_permission_mode"] == "plan"
    adv = sc["permission_advisory"]
    assert adv["autonomy"] == "blocked"
    assert adv["command"] == (
        "cremind tools set-var claude_code "
        "CLAUDE_CODE_PERMISSION_MODE=bypassPermissions"
    )


def test_autonomous_mode_has_no_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    _quick_result(monkeypatch, result="Done.", num_turns=2, session_id="sess-pm")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-pm-2", _profile="default",
        _variables={r.Var.PERMISSION_MODE: "bypassPermissions"},
    ))
    sc = res.structured_content
    assert sc["effective_permission_mode"] == "bypassPermissions"
    assert "permission_advisory" not in sc


def test_default_mode_when_unset(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    _quick_result(monkeypatch, result="Done.", num_turns=2, session_id="sess-pm")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-pm-3", _profile="default", _variables={},
    ))
    sc = res.structured_content
    # Unset variable resolves to the shipped default, which is autonomous.
    assert sc["effective_permission_mode"] == "bypassPermissions"
    assert "permission_advisory" not in sc


def test_empty_resume_guard_replaces_bare_empty_result(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    _quick_result(monkeypatch, result="", num_turns=0, session_id="sess-resume")

    res = asyncio.run(_run_tool(
        prompt="continue", working_directory=str(tmp_path),
        _context_id="conv-pm-4", _profile="default",
        session_id="sess-resume", _variables={},
    ))
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["resume_produced_no_work"] is True
    assert sc["result"].strip()  # not a bare empty string
    assert "without" in sc["result"].lower()
    assert "session_id" in sc["result"]


def test_empty_result_with_turns_passes_through(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    # A legitimate empty final message WITH real turns must not be rewritten.
    _quick_result(monkeypatch, result="", num_turns=3, session_id="sess-pm")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-pm-5", _profile="default", _variables={},
    ))
    sc = res.structured_content
    assert sc["result"] == ""
    assert "resume_produced_no_work" not in sc


def test_running_heartbeat_carries_mode_and_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import claude_code_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(client):
        yield SystemMessage(subtype="init", data={"session_id": "sess-hb"})
        await client.gate.wait()  # keep it "running" past the grace window
        yield ResultMessage(result="done", session_id="sess-hb")

    install_fake_sdk(monkeypatch, produce)

    async def body():
        run_res = await _run_tool(
            prompt="long job", working_directory=str(tmp_path),
            _context_id="conv-pm-6", _profile="default",
            _variables={r.Var.PERMISSION_MODE: "plan"},
        )
        sc = run_res.structured_content
        assert sc["status"] == "running"
        assert sc["effective_permission_mode"] == "plan"
        assert sc["permission_advisory"]["autonomy"] == "blocked"
        # release + drain so the background task doesn't outlive the test
        task = r.get_task(sc["task_id"])
        task.client.gate.set()
        await r.wait_for_task(task, 5)

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
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    assert _build_coding_delegation_guidance([]) == ""

    class Group:
        config_name = "claude_code"
        tool_id = "claude_code"

    text = _build_coding_delegation_guidance([Group()])
    assert "claude_code__run" in text
    assert "claude_code__wait" in text
    assert "claude_code__stop" in text
    assert "claude_code__status" in text
    # Claude-only guidance must not leak the peer agent's functions.
    assert "codex__" not in text
    # The permission playbook must be present and steer toward the real lever,
    # not the wrong "exit plan mode in your UI" remediation the incident produced.
    assert "cremind tools set-var" in text
    assert "ask the user ONCE" in text
    assert "plan mode" in text  # named in the negation ("no UI 'plan mode' ...")
    # No skill-creator enabled -> nothing to arbitrate between, so the
    # skill-authoring clause must not appear.
    assert "SKILL AUTHORING" not in text


def _skill_creator_group(tool_id: str = "default__skill_creator"):
    """Stand-in for the enabled skill-creator skill as the registry exposes it:
    ``ToolType.SKILL`` plus the profile-prefixed tool_id the model calls."""
    from app.tools import ToolType

    return types.SimpleNamespace(tool_type=ToolType.SKILL, tool_id=tool_id)


def test_guidance_adds_skill_authoring_choice_with_skill_creator():
    # Claude Code and skill-creator both enabled: the two rules collide (delegate
    # all file writing vs. scaffold the skill yourself), so the prompt must tell
    # the model to ask the user which one to use — naming the real functions.
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    class Group:
        config_name = "claude_code"
        tool_id = "claude_code"

    text = _build_coding_delegation_guidance([Group(), _skill_creator_group()])
    assert "SKILL AUTHORING" in text
    assert "`default__skill_creator`" in text        # path (a): the real skill fn
    assert "Claude Code (`claude_code__run`)" in text  # path (b): the real run fn
    assert "Codex" not in text                       # only the enabled delegate
    assert "target='skills'" in text                 # cwd before delegating
    assert "skill-creator/references/spec.md" in text
    assert "validate.py" in text
    assert "cremind skill-events events" in text
    # The delegation body itself is untouched by the clause.
    assert "claude_code__wait" in text


def test_guidance_absent_without_a_delegate_even_with_skill_creator():
    # The clause is a delegation rule: with no coding agent enabled there is
    # nothing to delegate to, and the whole block stays empty so the prompt is
    # byte-identical for profiles that only run skills.
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    assert _build_coding_delegation_guidance([_skill_creator_group()]) == ""


def test_skill_creator_matched_by_slug_not_a_hardcoded_id():
    # Skills register as ``<profile>__<slug>``; a non-default profile (or a
    # pre-prefix install carrying the bare slug) must match just the same, and an
    # unrelated skill must not.
    from app.agent.reasoning_agent import _skill_creator_tool_id

    assert _skill_creator_tool_id([_skill_creator_group("work__skill_creator")]) == (
        "work__skill_creator"
    )
    assert _skill_creator_tool_id([_skill_creator_group("skill_creator")]) == "skill_creator"
    assert _skill_creator_tool_id([_skill_creator_group("default__gmail")]) is None


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
    # The remediation points at the CLI's own sign-in (and the API-key escape
    # hatch), never at Settings -> LLM Providers, which is a different account.
    assert "claude auth login" in sc["remediation"]
    assert "cremind tools coding-agents login claude_code" in sc["remediation"]
    assert "LLM Providers" not in sc["remediation"]


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


def _no_sdk_stream(monkeypatch):
    """Install the fake SDK for a leaf that never starts a coding session."""
    async def produce(client):
        if False:
            yield

    return install_fake_sdk(monkeypatch, produce)


def _status_result(**overrides):
    """An :func:`auth_status` return value (its 7 keys), overridable."""
    payload = {
        "logged_in": None,
        "auth_method": None,
        "api_provider": None,
        "email": None,
        "org_name": None,
        "subscription_type": None,
        "detail": "",
    }
    payload.update(overrides)
    return payload


def _patch_auth_status(monkeypatch, result):
    from app.tools.builtin import claude_code_runner as r

    async def _status(variables, profile, *, config_dir=None, timeout=15.0):
        return dict(result)

    monkeypatch.setattr(r, "auth_status", _status)


def test_status_no_credentials(monkeypatch, _clean_registry):
    _no_sdk_stream(monkeypatch)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["available"] is True
    assert sc["credentials_configured"] is False
    assert sc["credential_source"] is None
    # No login anywhere -> no scope and no account, but the home is still named:
    # "where would a sign-in land?" is the next question the user has.
    assert sc["credential_scope"] is None
    assert sc["account_hint"] is None
    assert sc["cli_home"] == str(_clean_registry.shared)
    assert "probe=true" in sc["message"]
    assert "cremind tools coding-agents login claude_code" in sc["message"]


def test_status_with_tool_variable_key(monkeypatch, _clean_registry):
    _no_sdk_stream(monkeypatch)
    res = asyncio.run(
        _status_tool(_profile="default", _variables={"CLAUDE_CODE_API_KEY": "sk-test"})
    )
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "tool_variable_api_key"
    # A key has no login scope to sign out of, but it still runs in a CLI home.
    assert sc["credential_scope"] is None
    assert sc["cli_home"] == str(_clean_registry.shared)


def test_status_with_a_pasted_setup_token(monkeypatch, _clean_registry):
    """A `claude setup-token` token pasted into the tool variable is a credential.

    It is the only door left on a server no browser can reach, so "is Claude
    Code set up?" has to say yes for it - and has to name it, not the API key
    beside it: the CLI prefers the token when both are exported, so naming the
    key would describe a credential the run will not use.
    """
    _no_sdk_stream(monkeypatch)
    res = asyncio.run(_status_tool(
        _profile="default",
        _variables={
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-pasted",
            "CLAUDE_CODE_API_KEY": "sk-test",
        },
    ))
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "tool_variable_oauth_token"
    # A token, like a key, has no login scope to sign out of.
    assert sc["credential_scope"] is None
    assert sc["cli_home"] == str(_clean_registry.shared)


def test_every_variable_has_a_default_and_the_secrets_are_marked():
    """The schema and the runner's defaults are one thing seen from two sides.

    A variable declared in ``TOOL_CONFIG`` but missing from ``VAR_DEFAULTS`` is
    absent from every merged variable dict, so the feature it configures reads
    as unset until someone sets it by hand. And a credential variable without
    ``secret`` is rendered - and read back - in the clear.
    """
    from app.tools.builtin.claude_code import TOOL_CONFIG
    from app.tools.builtin.claude_code_runner import VAR_DEFAULTS

    declared = TOOL_CONFIG["required_config"]
    assert set(declared) == set(VAR_DEFAULTS)
    for name in ("CLAUDE_CODE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert declared[name]["secret"] is True
        assert declared[name]["default"] == ""
    # The token's description is where a user finds the browser-free door, so
    # it must name the command that produces one.
    assert "setup-token" in declared["CLAUDE_CODE_OAUTH_TOKEN"]["description"]


def test_status_probe_authenticated(monkeypatch):
    """The probe reports the CLI's own answer, account and all."""
    _no_sdk_stream(monkeypatch)
    _patch_auth_status(monkeypatch, _status_result(
        logged_in=True,
        auth_method="claude.ai",
        api_provider="firstParty",
        email="dev@example.com",
        org_name="Acme",
        subscription_type="max",
        detail="Signed in as dev@example.com (claude.ai, max).",
    ))
    res = asyncio.run(_status_tool(probe=True, _profile="default", _variables={}))
    sc = res.structured_content
    assert sc["logged_in"] is True
    assert sc["account"] == {
        "auth_method": "claude.ai",
        "email": "dev@example.com",
        "org_name": "Acme",
        "subscription_type": "max",
    }
    assert "ready" in sc["message"].lower()
    assert "dev@example.com" in sc["message"]


def test_status_probe_not_authenticated(monkeypatch):
    _no_sdk_stream(monkeypatch)
    _patch_auth_status(monkeypatch, _status_result(
        logged_in=False, detail="The Claude CLI reports no login in /tmp/home.",
    ))
    res = asyncio.run(_status_tool(probe=True, _profile="default", _variables={}))
    sc = res.structured_content
    assert sc["logged_in"] is False
    assert "not authenticated" in sc["message"].lower()
    assert "claude auth login" in sc["message"]
    assert sc["account"] is None


def test_status_probe_unknown_is_not_signed_out(monkeypatch):
    """``logged_in`` None means "cannot tell": the message must not claim the
    user is signed out (that sends them to fix a login that may be fine)."""
    _no_sdk_stream(monkeypatch)
    _patch_auth_status(monkeypatch, _status_result(
        detail="`claude auth status` did not answer within 15s.",
    ))
    res = asyncio.run(_status_tool(probe=True, _profile="default", _variables={}))
    sc = res.structured_content
    assert sc["logged_in"] is None
    assert "not the same as being signed out" in sc["message"]
    assert "did not answer" in sc["probe_detail"]


def test_probe_runs_no_billed_query(monkeypatch):
    """The probe must never start a coding session.

    The previous implementation ran a real one-turn SDK query, so every
    "Check sign-in" click cost money on the user's Anthropic account. Nothing
    may construct an SDK client on this path.
    """
    mod = _no_sdk_stream(monkeypatch)
    constructed = []
    original_init = mod.ClaudeSDKClient.__init__

    def _init(self, options=None):
        constructed.append(options)
        original_init(self, options)

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)
    _patch_auth_status(monkeypatch, _status_result(logged_in=True, detail="ok"))

    res = asyncio.run(_status_tool(probe=True, _profile="default", _variables={}))
    assert res.structured_content["logged_in"] is True
    assert constructed == []


def test_status_host_claude_login(monkeypatch, _clean_registry):
    """The server's shared CLI login is visible to a profile that has none of its
    own, reported as ``host_claude_login`` with ``shared`` scope, which is what
    tells the user they are borrowing the operator's account."""
    _clean_registry.shared.mkdir(parents=True, exist_ok=True)
    (_clean_registry.shared / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "host-tok"}}', encoding="utf-8",
    )
    (_clean_registry.shared / ".claude.json").write_text(
        '{"oauthAccount": {"emailAddress": "ops@example.com", "organizationName": "Acme"}}',
        encoding="utf-8",
    )

    _no_sdk_stream(monkeypatch)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "host_claude_login"
    assert sc["credential_scope"] == "shared"
    assert sc["cli_home"] == str(_clean_registry.shared)
    assert sc["account_hint"] == {
        "type": "oauth", "email": "ops@example.com", "org_name": "Acme",
    }


def test_status_profile_claude_login(monkeypatch, _clean_registry):
    """A profile that signed in itself reports its own home and ``profile``
    scope, even while the server's shared login also exists."""
    _clean_registry.shared.mkdir(parents=True, exist_ok=True)
    (_clean_registry.shared / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "host-tok"}}', encoding="utf-8",
    )
    own = _clean_registry.system_dir / "alice" / "coding-cli" / "claude"
    own.mkdir(parents=True)
    (own / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "alice-tok"}}', encoding="utf-8",
    )

    _no_sdk_stream(monkeypatch)
    res = asyncio.run(_status_tool(_profile="alice", _variables={}))
    sc = res.structured_content
    assert sc["credential_source"] == "profile_claude_login"
    assert sc["credential_scope"] == "profile"
    assert sc["cli_home"] == str(own)


def test_status_includes_models(monkeypatch):
    from app.tools.builtin import claude_code_runner as r

    async def _models(variables, profile, *, force_refresh=False):
        return {
            "models": [
                {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
                {"id": "claude-opus-4-5", "display_name": "Opus 4.5"},
            ],
            "source": "host_claude_login",
            "cached": False,
        }

    monkeypatch.setattr(r, "list_models", _models)

    async def produce(client):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    ids = [m["id"] for m in sc["models"]]
    assert ids == ["claude-sonnet-4-5", "claude-opus-4-5"]
    assert not sc.get("models_error")
    assert "cremind tools options claude_code" in sc["models_hint"]


def test_status_models_error_passthrough(monkeypatch):
    from app.tools.builtin import claude_code_runner as r

    async def _models(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "credential rejected", "source": None}

    monkeypatch.setattr(r, "list_models", _models)

    async def produce(client):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _status_tool(_profile="default", _variables={"CLAUDE_CODE_API_KEY": "sk-bad"})
    )
    sc = res.structured_content
    assert sc["models"] == []
    assert sc["models_error"] == "credential rejected"
    # The credential-source status is independent of the model fetch outcome.
    assert sc["credential_source"] == "tool_variable_api_key"
