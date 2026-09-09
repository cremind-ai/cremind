"""Unit tests for the ``codex`` built-in tool.

Exercises the SDK bridge with a fake ``openai_codex`` injected into
``sys.modules`` (no real SDK / binary / network), plus the missing-SDK gate,
concurrency limits, the notification→activity translator, and the
feature/guidance wiring.

Tests drive coroutines with ``asyncio.run`` (matching the repo's other tool
tests — no pytest-asyncio needed). The module-level task registry is cleared
around every test by an autouse fixture.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest


# ── fake Codex SDK notification / payload / item types ────────────────────────
@dataclass
class Notification:
    method: str
    payload: Any


@dataclass
class Breakdown:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ThreadTokenUsage:
    last: Optional[Breakdown] = None
    total: Optional[Breakdown] = None
    model_context_window: Optional[int] = None


@dataclass
class TurnError:
    message: str = ""


@dataclass
class Turn:
    status: Any
    items: list = field(default_factory=list)
    error: Optional[TurnError] = None
    duration_ms: Optional[int] = None


@dataclass
class Change:
    path: str = ""


@dataclass
class Item:
    """A stand-in for any Codex ThreadItem variant (duck-typed by ``type``)."""

    type: str
    id: str = ""
    text: str = ""
    phase: Any = None
    command: str = ""
    aggregated_output: str = ""
    exit_code: Any = None
    status: Any = None
    changes: Optional[list] = None
    query: str = ""
    server: str = ""
    tool: str = ""
    arguments: Any = None
    summary: Any = None
    error: Any = None
    success: Any = None


def _note(method: str, **payload_fields) -> Notification:
    return Notification(method, SimpleNamespace(**payload_fields))


def install_fake_sdk(
    monkeypatch,
    produce,
    *,
    aenter_error: Optional[Exception] = None,
    account_resp: Any = None,
    models_resp: Any = None,
    thread_id: str = "thread-1",
    login_handle: Any = None,
):
    """Install a fake ``openai_codex`` whose turn streams ``produce(handle)``.

    ``login_handle`` is what ``login_chatgpt_device_code()`` returns (the
    device-code sign-in in :mod:`app.tools.builtin.codex_login` drives it), and
    ``logout()`` records that it was called on the instance."""
    import enum as _enum

    mod = types.ModuleType("openai_codex")

    @dataclass
    class CodexConfig:
        codex_bin: Any = None
        launch_args_override: Any = None
        config_overrides: tuple = ()
        cwd: Any = None
        env: Any = None
        client_name: str = "codex_python_sdk"

    class Sandbox(str, _enum.Enum):
        read_only = "read-only"
        workspace_write = "workspace-write"
        full_access = "full-access"

    class ApprovalMode(str, _enum.Enum):
        deny_all = "deny_all"
        auto_review = "auto_review"

    class AsyncTurnHandle:
        def __init__(self, produce_fn):
            self._produce = produce_fn
            self.gate = asyncio.Event()
            self.interrupted = False

        def stream(self):
            return self._produce(self)

        async def interrupt(self):
            self.interrupted = True
            self.gate.set()

    class AsyncThread:
        def __init__(self, thread_id_, produce_fn):
            self.id = thread_id_
            self._produce = produce_fn

        async def turn(self, prompt, *, effort=None, model=None, **kw):
            self.prompt = prompt
            return AsyncTurnHandle(self._produce)

    class AsyncCodex:
        instances: list = []

        def __init__(self, config=None):
            self.config = config
            self.logged_in_key = None
            self.logged_out = False
            AsyncCodex.instances.append(self)

        async def __aenter__(self):
            if aenter_error is not None:
                raise aenter_error
            return self

        async def __aexit__(self, *exc):
            return False

        async def login_api_key(self, key):
            self.logged_in_key = key

        async def login_chatgpt_device_code(self):
            return login_handle

        async def logout(self):
            self.logged_out = True

        async def account(self):
            return account_resp

        async def models(self):
            return models_resp

        async def thread_start(self, *, cwd=None, model=None, sandbox=None,
                               approval_mode=None, developer_instructions=None, **kw):
            return AsyncThread(thread_id, produce)

        async def thread_resume(self, session_id, *, cwd=None, model=None, sandbox=None,
                                approval_mode=None, developer_instructions=None, **kw):
            return AsyncThread(session_id, produce)

    mod.CodexConfig = CodexConfig
    mod.Sandbox = Sandbox
    mod.ApprovalMode = ApprovalMode
    mod.AsyncCodex = AsyncCodex
    mod.AsyncThread = AsyncThread
    mod.AsyncTurnHandle = AsyncTurnHandle
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    """Isolate the module registry, keep the activity feed hermetic, and
    neutralise ambient credentials. ``list_models`` is stubbed to an empty
    success so the status leaf never spawns a real client.

    Credential isolation is the ordinary environment one now, not a patch of
    private module constants: ``CODEX_HOME`` names the shared CLI home and
    ``CREMIND_SYSTEM_DIR`` the per-profile ones, both under ``tmp_path``, so
    ``resolve_auth`` can reach neither the developer's own ``codex login`` nor
    their real system directory. ``is_container`` is pinned False so the
    container-only legacy ``~/.codex`` tier stays out of the way when the suite
    itself runs inside a container.
    """
    from app.config import runtime_env
    from app.tools.builtin import codex_runner as r
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
    monkeypatch.setattr(r.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path / "system"))
    monkeypatch.setattr(runtime_env, "is_container", lambda *a, **k: False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "shared-codex"))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(r, "list_models", _empty_models)
    yield
    r._task_registry.clear()
    r._models_cache.clear()


def _run_tool(**args):
    from app.tools.builtin.codex import CodexRunTool

    return CodexRunTool().run(args)


def _status_tool(**args):
    from app.tools.builtin.codex import CodexStatusTool

    return CodexStatusTool().run(args)


# ── missing SDK ───────────────────────────────────────────────────────────────
def test_missing_sdk_returns_missing_dependency(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    res = asyncio.run(
        _run_tool(prompt="do a thing", working_directory=str(tmp_path), _variables={})
    )
    sc = res.structured_content
    assert sc["error"] == "MissingDependency"
    assert sc["feature_key"] == "codex"
    assert "codex" in sc["extras"]


# ── fast task completes within the grace window ───────────────────────────────
def test_fast_task_completes_in_grace_window(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-42"))
        yield _note("item/started", item=Item(type="commandExecution", id="c1", command="make"))
        yield _note("item/completed", item=Item(
            type="commandExecution", id="c1", command="make",
            status="completed", exit_code=0, aggregated_output="ok",
        ))
        yield _note("thread/tokenUsage/updated",
                    token_usage=ThreadTokenUsage(last=Breakdown(10, 5, 20),
                                                 model_context_window=272_000))
        yield _note("turn/completed", turn=Turn(
            status="completed", duration_ms=5000,
            items=[Item(type="agentMessage", text="Done. Created a.py", phase="final_answer")],
        ))

    install_fake_sdk(monkeypatch, produce, thread_id="thread-42")
    res = asyncio.run(
        _run_tool(prompt="create a.py", working_directory=str(tmp_path),
                  _context_id="conv-1", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["result"] == "Done. Created a.py"
    assert sc["session_id"] == "thread-42"
    assert sc["duration_ms"] == 5000
    # codex is a delegated sub-agent: usage is NOT folded into the turn (shown only in
    # the Agent Activity panel). The model-visible payload still carries the split,
    # where Codex's input_tokens (incl. cached) is split into input − cached.
    assert res.token_usage is None
    assert sc["usage"] == {
        "input_tokens": 5, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0,
    }
    # Live context usage reached the activity snapshot: last.input_tokens verbatim
    # (cached NOT subtracted here), window from the SDK's model_context_window.
    task = r.get_task(sc["task_id"])
    assert task is not None
    assert task.activity.snapshot()["usage"] == {
        "context_tokens": 10, "context_window": 272_000,
    }


# ── sandbox awareness in the result payloads ─────────────────────────────────
def _one_turn(result_text="Done.", *, thread_id="thread-sb", items=None):
    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id=thread_id))
        turn_items = items if items is not None else [
            Item(type="agentMessage", text=result_text, phase="final_answer"),
        ]
        yield _note("turn/completed", turn=Turn(
            status="completed", duration_ms=100, items=turn_items,
        ))
    return produce


def test_read_only_sandbox_carries_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    install_fake_sdk(monkeypatch, _one_turn("Here's what I found."), thread_id="thread-sb")

    res = asyncio.run(_run_tool(
        prompt="review this", working_directory=str(tmp_path),
        _context_id="conv-sb-1", _profile="default",
        _variables={r.Var.SANDBOX: "read-only"},
    ))
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["effective_sandbox"] == "read-only"
    adv = sc["sandbox_advisory"]
    assert adv["autonomy"] == "blocked"
    assert adv["command"] == "cremind tools set-var codex CODEX_SANDBOX=full-access"


def test_full_access_sandbox_has_no_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    install_fake_sdk(monkeypatch, _one_turn("Done."), thread_id="thread-sb")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-2", _profile="default",
        _variables={r.Var.SANDBOX: "full-access"},
    ))
    sc = res.structured_content
    assert sc["effective_sandbox"] == "full-access"
    assert "sandbox_advisory" not in sc


def test_empty_resume_guard(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    # A resumed thread that yields no final agent message → empty result.
    install_fake_sdk(monkeypatch, _one_turn(items=[]), thread_id="sess-resume")

    res = asyncio.run(_run_tool(
        prompt="continue", working_directory=str(tmp_path),
        _context_id="conv-sb-3", _profile="default",
        session_id="sess-resume", _variables={},
    ))
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["resume_produced_no_work"] is True
    assert sc["result"].strip()
    assert "session_id" in sc["result"]


def test_empty_result_fresh_no_resume_guard(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    # A FRESH (non-resumed) turn that yields no final message must pass through
    # untouched — the guard only fires for a resume attempt.
    install_fake_sdk(monkeypatch, _one_turn(items=[]), thread_id="thread-fresh")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-fresh", _profile="default", _variables={},
    ))
    sc = res.structured_content
    assert sc["status"] == "completed"
    assert sc["result"] == ""
    assert "resume_produced_no_work" not in sc


def test_unset_sandbox_defaults_full_access_no_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    install_fake_sdk(monkeypatch, _one_turn("Done."), thread_id="thread-sb")

    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-unset", _profile="default", _variables={},
    ))
    sc = res.structured_content
    assert sc["effective_sandbox"] == "full-access"
    assert "sandbox_advisory" not in sc
    assert "sandbox_coercion_note" not in sc


def test_invalid_sandbox_reports_full_access_with_note(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    install_fake_sdk(monkeypatch, _one_turn("Done."), thread_id="thread-sb")

    # A garbage sandbox coerces to full-access at run time; the report must match
    # the real run (no misleading "blocked" advisory) and flag the fall-open.
    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-bad", _profile="default",
        _variables={r.Var.SANDBOX: "readonly"},
    ))
    sc = res.structured_content
    assert sc["effective_sandbox"] == "full-access"
    assert "sandbox_advisory" not in sc
    assert "readonly" in sc["sandbox_coercion_note"]


def test_config_override_sandbox_read_only_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    install_fake_sdk(monkeypatch, _one_turn("Reviewed."), thread_id="thread-sb")

    # A sandbox_mode override in CODEX_CONFIG_OVERRIDES is the real effective
    # sandbox — the advisory must reflect it even though CODEX_SANDBOX is full.
    res = asyncio.run(_run_tool(
        prompt="review", working_directory=str(tmp_path),
        _context_id="conv-sb-ovr", _profile="default",
        _variables={r.Var.SANDBOX: "full-access",
                    r.Var.CONFIG_OVERRIDES: "sandbox_mode=read-only"},
    ))
    sc = res.structured_content
    assert sc["effective_sandbox"] == "read-only"
    assert sc["sandbox_advisory"]["autonomy"] == "blocked"


def test_running_heartbeat_carries_sandbox_advisory(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-hb"))
        await handle.gate.wait()  # keep it "running" past the grace window
        yield _note("turn/completed", turn=Turn(
            status="completed", duration_ms=1,
            items=[Item(type="agentMessage", text="done", phase="final_answer")],
        ))

    install_fake_sdk(monkeypatch, produce, thread_id="thread-hb")

    async def body():
        run_res = await _run_tool(
            prompt="long job", working_directory=str(tmp_path),
            _context_id="conv-sb-hb", _profile="default",
            _variables={r.Var.SANDBOX: "read-only"},
        )
        sc = run_res.structured_content
        assert sc["status"] == "running"
        assert sc["effective_sandbox"] == "read-only"
        assert sc["sandbox_advisory"]["autonomy"] == "blocked"
        # release + drain so the background task doesn't outlive the test
        task = r.get_task(sc["task_id"])
        task.turn_handle.gate.set()
        await r.wait_for_task(task, 5)

    asyncio.run(body())


def test_login_failure_surfaces_not_silent(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)
    mod = install_fake_sdk(monkeypatch, _one_turn("Done."), thread_id="thread-sb")

    async def _boom(self, key):
        raise RuntimeError("login failed: boom")

    mod.AsyncCodex.login_api_key = _boom

    # With an explicit key, a login failure must surface as a failed run (not be
    # swallowed and silently proceed against a stale/empty store).
    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-login", _profile="default",
        _variables={r.Var.API_KEY: "sk-explicit"},
    ))
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "CodexError"


def test_auth_error_classified_from_error_code(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 1.0)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-auth"))
        # Auth reason only in .code (not .message) — must still classify as auth,
        # and must NOT be mislabeled a sandbox problem despite a restrictive sandbox.
        yield _note("turn/completed", turn=Turn(
            status="failed", duration_ms=1,
            error=SimpleNamespace(message="", code="invalid_api_key"),
        ))

    install_fake_sdk(monkeypatch, produce, thread_id="thread-auth")
    res = asyncio.run(_run_tool(
        prompt="do it", working_directory=str(tmp_path),
        _context_id="conv-sb-authcode", _profile="default",
        _variables={r.Var.SANDBOX: "read-only"},
    ))
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "AuthenticationError"
    assert "sandbox_advisory" not in sc


# Captured at import time, BEFORE the autouse fixture stubs runner.list_models,
# so the timeout test can exercise the real implementation.
from app.tools.builtin.codex_runner import list_models as _real_list_models  # noqa: E402


def test_resolve_auth_isolates_home_per_credential(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    # (the autouse fixture already nulls the env keys and points both homes at
    # tmp_path)
    a_aaa = r.resolve_auth({r.Var.API_KEY: "sk-AAA"}, "admin")
    a_bbb = r.resolve_auth({r.Var.API_KEY: "sk-BBB"}, "admin")
    a_aaa2 = r.resolve_auth({r.Var.API_KEY: "sk-AAA"}, "admin")
    # Distinct keys -> distinct CODEX_HOME; same key -> same CODEX_HOME.
    assert a_aaa.env_overrides["CODEX_HOME"] != a_bbb.env_overrides["CODEX_HOME"]
    assert a_aaa.env_overrides["CODEX_HOME"] == a_aaa2.env_overrides["CODEX_HOME"]
    # No key -> the CLI login home, ALWAYS named. Leaving CODEX_HOME unset used
    # to mean "whatever ~/.codex the server user has", which on a multi-profile
    # server is the one home no profile's run may land in.
    assert r.resolve_auth({}, "admin").env_overrides == {
        "CODEX_HOME": str(tmp_path / "shared-codex"),
    }


def test_list_models_times_out_gracefully(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_MODELS_TIMEOUT", 0.05)
    mod = install_fake_sdk(monkeypatch, _one_turn("x"))

    async def _slow_models(self):
        await asyncio.sleep(0.5)
        return SimpleNamespace(data=[])

    mod.AsyncCodex.models = _slow_models

    out = asyncio.run(_real_list_models({r.Var.API_KEY: "sk-x"}, "admin"))
    assert out["models"] == []
    assert "timed out" in out["error"]


# ── slow task: handle → wait → final; usage folded exactly once ───────────────
def test_slow_task_handle_then_wait_final_usage_once(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r
    from app.tools.builtin.codex import CodexWaitTool

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-slow"))
        yield _note("item/started", item=Item(type="commandExecution", id="c1", command="make"))
        await handle.gate.wait()  # block until the test releases it
        yield _note("thread/tokenUsage/updated",
                    token_usage=ThreadTokenUsage(last=Breakdown(1, 0, 2)))
        yield _note("turn/completed", turn=Turn(
            status="completed", duration_ms=99,
            items=[Item(type="agentMessage", text="Built the project", phase="final_answer")],
        ))

    install_fake_sdk(monkeypatch, produce, thread_id="thread-slow")

    async def body():
        run_res = await _run_tool(
            prompt="build it", working_directory=str(tmp_path),
            _context_id="conv-2", _profile="default", _variables={},
        )
        assert run_res.structured_content["status"] == "running"
        task_id = run_res.structured_content["task_id"]

        task = r.get_task(task_id)
        assert task is not None and task.turn_handle is not None
        task.turn_handle.gate.set()  # release the blocked stream

        wait_res = await CodexWaitTool().run({"task_id": task_id, "timeout": 5})
        assert wait_res.structured_content["status"] == "completed"
        assert wait_res.structured_content["result"] == "Built the project"
        # Delegated sub-agent usage is never folded into the turn; the model-visible
        # payload still carries the split.
        assert wait_res.token_usage is None
        assert wait_res.structured_content["usage"] == {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        # ...and NOT again on a re-issued wait.
        again = await CodexWaitTool().run({"task_id": task_id, "timeout": 5})
        assert again.token_usage is None

    asyncio.run(body())


# ── stop interrupts a hung task; session stays resumable ──────────────────────
def test_stop_interrupts_resumable(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-hang"))
        never = asyncio.Event()
        await never.wait()  # hang; interrupt() (overridden below) won't complete it
        yield _note("turn/completed", turn=Turn(status="completed"))  # unreachable

    mod = install_fake_sdk(monkeypatch, produce, thread_id="thread-hang")

    async def _noop_interrupt(self):
        self.interrupted = True

    mod.AsyncTurnHandle.interrupt = _noop_interrupt

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
        assert task.result["session_id"] == "thread-hang"

    asyncio.run(body())


# ── graceful interrupt: turn completes with status=interrupted ─────────────────
def test_interrupt_completes_as_interrupted(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="thread-x"))
        await handle.gate.wait()  # interrupt() sets the gate for this handle
        yield _note("turn/completed", turn=Turn(status="interrupted"))

    install_fake_sdk(monkeypatch, produce, thread_id="thread-x")

    async def body():
        run_res = await _run_tool(
            prompt="loop", working_directory=str(tmp_path),
            _context_id="conv-3b", _profile="default", _variables={},
        )
        task = r.get_task(run_res.structured_content["task_id"])
        status = await r.stop_task(task, grace=2.0)
        assert status == "interrupted"
        assert task.result["session_id"] == "thread-x"

    asyncio.run(body())


# ── one task per conversation ─────────────────────────────────────────────────
def test_one_task_per_conversation(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.2)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="s"))
        await handle.gate.wait()
        yield _note("turn/completed", turn=Turn(status="completed"))

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


# ── global concurrency cap ────────────────────────────────────────────────────
def test_global_max_concurrent(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 0.1)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="s"))
        await handle.gate.wait()
        yield _note("turn/completed", turn=Turn(status="completed"))

    install_fake_sdk(monkeypatch, produce)

    async def body():
        a = await _run_tool(prompt="a", working_directory=str(tmp_path),
                            _context_id="cA", _variables={"CODEX_MAX_CONCURRENT_TASKS": 1})
        assert a.structured_content["status"] == "running"
        b = await _run_tool(prompt="b", working_directory=str(tmp_path),
                            _context_id="cB", _variables={"CODEX_MAX_CONCURRENT_TASKS": 1})
        assert b.structured_content["error"] == "TooManyTasks"

    asyncio.run(body())


# ── wait on a bogus id ────────────────────────────────────────────────────────
def test_wait_unknown_task_id(monkeypatch):
    from app.tools.builtin.codex import CodexWaitTool

    res = asyncio.run(CodexWaitTool().run({"task_id": "deadbeef"}))
    sc = res.structured_content
    assert sc["error"] == "TaskNotFound"
    assert "known_task_ids" in sc


# ── activity translator (pure; no SDK) ────────────────────────────────────────
def test_item_label_mapping():
    from app.tools.builtin.codex_activity import item_label

    assert item_label(Item(type="commandExecution", command="ls -la"))[0] == "$ ls -la"
    assert item_label(Item(type="fileChange", changes=[Change(path="x.py")]))[0] == "Editing x.py"
    assert item_label(Item(type="fileChange", changes=[Change(path="a"), Change(path="b")]))[0] == "Editing 2 files"
    assert item_label(Item(type="webSearch", query="pydantic"))[0] == "Searching web: pydantic"
    assert item_label(Item(type="mcpToolCall", server="fs", tool="read"))[0] == "Tool: fs.read"
    assert item_label(Item(type="dynamicToolCall", tool="fetch"))[0] == "Tool: fetch"


def test_apply_notification_builds_steps():
    from app.tools.builtin.codex_activity import apply_notification

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
        await apply_notification(act, _note(
            "item/started", item=Item(type="commandExecution", id="c1", command="make")))
        await apply_notification(act, _note(
            "item/completed", item=Item(type="commandExecution", id="c1",
                                        status="completed", exit_code=0, aggregated_output="ok")))
        await apply_notification(act, _note(
            "item/completed", item=Item(type="reasoning", summary=["thinking hard"])))
        await apply_notification(act, _note(
            "item/completed", item=Item(type="agentMessage", text="all done", phase="final_answer")))
        # A failed command resolves as error.
        await apply_notification(act, _note(
            "item/started", item=Item(type="commandExecution", id="c2", command="bad")))
        await apply_notification(act, _note(
            "item/completed", item=Item(type="commandExecution", id="c2",
                                        status="failed", exit_code=1)))
        kinds = [a[0] for a in act.added]
        assert kinds == ["tool_use", "thinking", "text", "tool_use"]
        assert ("tool_use", "$ make", "c1", "running") in act.added
        assert ("c1", "ok") in act.resolved
        assert ("c2", "error") in act.resolved

    asyncio.run(body())


def test_apply_notification_updates_context_usage():
    from app.tools.builtin.codex_activity import apply_notification

    class FakeActivity:
        def __init__(self):
            self.usage = None

        async def add_step(self, *, kind, label, detail=None, step_id=None, status=None):
            return step_id or "s1"

        async def resolve_step(self, step_id, *, status, detail_suffix=None):
            return None

        async def update_usage(self, usage):
            self.usage = usage

    async def body():
        # last.input_tokens is used verbatim — cached is NOT subtracted (that split
        # is only for cost accounting in the runner).
        act = FakeActivity()
        await apply_notification(act, _note(
            "thread/tokenUsage/updated",
            token_usage=ThreadTokenUsage(
                last=Breakdown(input_tokens=42_000, cached_input_tokens=30_000, output_tokens=900),
                model_context_window=272_000,
            )))
        assert act.usage == {"context_tokens": 42_000, "context_window": 272_000}

        # Unknown window → tokens only.
        no_window = FakeActivity()
        await apply_notification(no_window, _note(
            "thread/tokenUsage/updated",
            token_usage=ThreadTokenUsage(last=Breakdown(input_tokens=5_000))))
        assert no_window.usage == {"context_tokens": 5_000, "context_window": None}

    asyncio.run(body())


# ── auth-failure classification ───────────────────────────────────────────────
def test_failed_turn_auth_error_is_classified(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="s"))
        yield _note("turn/completed", turn=Turn(
            status="failed",
            error=TurnError(message="HTTP 401 Unauthorized: invalid api key"),
        ))

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "AuthenticationError"
    # The remediation names the doors that still exist. Settings -> LLM stopped
    # being one of them when the provider credential tiers were removed.
    assert "cremind tools coding-agents login codex" in sc["remediation"]
    assert "CODEX_API_KEY" in sc["remediation"]
    assert "Settings -> LLM" not in sc["remediation"]


def test_failed_turn_generic_stays_generic(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(handle):
        yield _note("thread/started", thread=SimpleNamespace(id="s"))
        yield _note("turn/completed", turn=Turn(
            status="failed", error=TurnError(message="compilation failed: syntax error"),
        ))

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["status"] == "failed"
    assert sc["error"] == "CodexError"


def test_raised_auth_exception_is_classified(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(handle):
        if False:
            yield  # make it an async generator
        raise RuntimeError("HTTP 401 Unauthorized: incorrect api key provided")

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    assert res.structured_content["error"] == "AuthenticationError"


def test_binary_not_found_is_classified(monkeypatch, tmp_path):
    from app.tools.builtin import codex_runner as r

    monkeypatch.setattr(r, "_RUN_GRACE_SECONDS", 2.0)

    async def produce(handle):
        if False:
            yield
        raise FileNotFoundError("codex binary missing")

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _run_tool(prompt="build", working_directory=str(tmp_path),
                  _context_id="c", _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["error"] == "CodexBinaryNotFound"
    assert "CODEX_BIN" in sc["remediation"]


# ── build_config: codex_bin must not become the literal "None" ────────────────
def test_build_config_codex_bin_none_safe(monkeypatch):
    """A missing/empty CODEX_BIN must yield ``codex_bin=None`` — not the string
    "None", which the real SDK would treat as a binary path and fail on. Reaches
    list_models / probe_auth, which may pass an unmerged variables dict."""
    from app.tools.builtin import codex_runner as r

    async def produce(handle):
        if False:
            yield

    mod = install_fake_sdk(monkeypatch, produce)
    auth = r.CodexAuth(env_overrides={}, api_key=None, source=None)
    for variables in ({}, {r.Var.BIN_PATH: None}, {r.Var.BIN_PATH: ""}, {r.Var.BIN_PATH: "  "}):
        cfg = r.build_config(mod, variables=variables, auth=auth, cwd=None)
        assert cfg.codex_bin is None, f"codex_bin should be None for {variables!r}"
    # A real path still passes through.
    cfg = r.build_config(mod, variables={r.Var.BIN_PATH: "/usr/bin/codex"}, auth=auth, cwd=None)
    assert cfg.codex_bin == "/usr/bin/codex"


# ── feature + default-disabled + guidance wiring ──────────────────────────────
def test_feature_and_pip_spec():
    from app.features.manifest import FEATURES, pip_spec

    feat = FEATURES["codex"]
    assert feat.extras == ("codex",)
    assert feat.probes == ("openai_codex",)
    assert feat.requires_restart is False
    assert pip_spec(["codex"]).startswith("cremind[codex]==")


def test_default_disabled_and_feature_gated():
    from app.tools.builtin.codex import TOOL_CONFIG

    assert TOOL_CONFIG["default"] is False
    assert TOOL_CONFIG["requires_feature"] == "codex"


def _skill_creator_group(tool_id: str = "default__skill_creator"):
    """Stand-in for the enabled skill-creator skill as the registry exposes it:
    ``ToolType.SKILL`` plus the profile-prefixed tool_id the model calls."""
    from app.tools import ToolType

    return SimpleNamespace(tool_type=ToolType.SKILL, tool_id=tool_id)


def test_guidance_codex_only():
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    class Group:
        config_name = "codex"
        tool_id = "codex"

    text = _build_coding_delegation_guidance([Group()])
    assert "codex__run" in text
    assert "codex__wait" in text
    assert "codex__stop" in text
    assert "codex__status" in text
    assert "claude_code__" not in text
    # No skill-creator enabled -> no skill-authoring clause to arbitrate.
    assert "SKILL AUTHORING" not in text


def test_guidance_codex_only_with_skill_creator():
    # Codex is the only delegate, so the "ask which path" clause must offer it
    # (and nothing else) as the delegation option.
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    class Group:
        config_name = "codex"
        tool_id = "codex"

    text = _build_coding_delegation_guidance([Group(), _skill_creator_group()])
    assert "SKILL AUTHORING" in text
    assert "`default__skill_creator`" in text
    assert "Codex (`codex__run`)" in text
    assert "Claude Code" not in text
    assert "target='skills'" in text
    assert "codex__wait" in text  # the delegation body is untouched


def test_guidance_both_agents():
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    class Claude:
        config_name = "claude_code"
        tool_id = "claude_code"

    class Codex:
        config_name = "codex"
        tool_id = "codex"

    text = _build_coding_delegation_guidance([Claude(), Codex()])
    assert "claude_code__run" in text
    assert "codex__run" in text
    # The peer-preference sentence is present when both are on.
    assert "peers" in text
    assert "SKILL AUTHORING" not in text


def test_guidance_both_agents_with_skill_creator():
    # With both delegates on, the choice the user is offered names both run
    # functions, and the brief points at "the chosen agent's run function"
    # rather than picking one for them.
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    class Claude:
        config_name = "claude_code"
        tool_id = "claude_code"

    class Codex:
        config_name = "codex"
        tool_id = "codex"

    text = _build_coding_delegation_guidance(
        [Claude(), Codex(), _skill_creator_group()]
    )
    assert "SKILL AUTHORING" in text
    assert "Claude Code (`claude_code__run`) or Codex (`codex__run`)" in text
    assert "the chosen agent's run function" in text
    assert "`default__skill_creator`" in text


def test_guidance_skill_creator_alone_stays_empty():
    # A profile with skill-creator but no coding delegate keeps the historical
    # (empty) prompt — the clause is a delegation rule, not a skills rule.
    from app.agent.reasoning_agent import _build_coding_delegation_guidance

    assert _build_coding_delegation_guidance([_skill_creator_group()]) == ""


# ── status leaf (answers "is Codex set up / logged in?") ──────────────────────
def test_status_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["available"] is False
    assert sc["sdk_installed"] is False


def test_status_no_credentials(monkeypatch):
    async def produce(handle):
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
    async def produce(handle):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(
        _status_tool(_profile="default", _variables={"CODEX_API_KEY": "sk-test"})
    )
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "tool_variable_api_key"


def test_status_host_codex_login(monkeypatch, tmp_path):
    """A login in the SHARED home is reported as the server's, not the profile's.

    The scope is the half that matters here: a profile borrowing the operator's
    login reads "signed in" exactly like one that signed in itself, and only
    the latter has anything it can sign out of.
    """
    shared = tmp_path / "shared-codex"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "auth.json").write_text('{"OPENAI_API_KEY": "host-key"}')

    async def produce(handle):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    assert sc["credentials_configured"] is True
    assert sc["credential_source"] == "host_codex_login"
    assert sc["credential_scope"] == "shared"
    assert sc["cli_home"] == str(shared)
    assert sc["account_hint"] == {"type": "api_key"}


def test_status_profile_codex_login_beats_the_shared_one(monkeypatch, tmp_path):
    """A profile that signed in itself authenticates from its OWN home."""
    from app.tools.builtin import codex_runner as r

    shared = tmp_path / "shared-codex"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "auth.json").write_text('{"OPENAI_API_KEY": "shared-key"}')
    own = r.profile_codex_home("lee")
    own.mkdir(parents=True, exist_ok=True)
    (own / "auth.json").write_text('{"tokens": {"access_token": "lee-token"}}')

    async def produce(handle):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(_status_tool(_profile="lee", _variables={}))
    sc = res.structured_content
    assert sc["credential_source"] == "profile_codex_login"
    assert sc["credential_scope"] == "profile"
    assert sc["cli_home"] == str(own)


def test_status_probe_ignores_requires_openai_auth(monkeypatch, tmp_path):
    """An account plus ``requires_openai_auth=True`` still means signed in.

    This is the shape the real bundled binary returns in EVERY reachable state
    — empty home, API-key home, ChatGPT home, even straight after the SDK's own
    ``login_api_key`` succeeded — so gating ``logged_in`` on the flag made it
    permanently False and told users to re-credential a credential Codex was
    actively using. ``account`` alone decides it; this pins that, because the
    two cases below both vary ``account`` as well and so cannot catch a
    regression here on their own.
    """
    async def produce(handle):
        if False:
            yield

    account = SimpleNamespace(account=SimpleNamespace(), requires_openai_auth=True)
    install_fake_sdk(monkeypatch, produce, account_resp=account)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={"CODEX_API_KEY": "sk-x"})
    )
    sc = res.structured_content
    assert sc["logged_in"] is True
    assert "ready" in sc["message"].lower()


def test_status_probe_authenticated(monkeypatch, tmp_path):
    async def produce(handle):
        if False:
            yield

    # ``account`` is a pydantic RootModel union in the real SDK, so the account
    # fields live under ``.root``; reading them off the wrapper would show a
    # signed-in account with no name on it.
    chatgpt = SimpleNamespace(type="chatgpt", email="lee@example.com", plan_type="pro")
    account = SimpleNamespace(
        account=SimpleNamespace(root=chatgpt), requires_openai_auth=False,
    )
    install_fake_sdk(monkeypatch, produce, account_resp=account)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={"CODEX_API_KEY": "sk-x"})
    )
    sc = res.structured_content
    assert sc["logged_in"] is True
    assert "ready" in sc["message"].lower()
    assert sc["account"] == {
        "type": "chatgpt", "email": "lee@example.com", "plan_type": "pro",
    }


def test_status_probe_reports_an_api_key_account(monkeypatch, tmp_path):
    """The SDK's camelCase discriminators are translated once, here."""
    async def produce(handle):
        if False:
            yield

    account = SimpleNamespace(
        account=SimpleNamespace(root=SimpleNamespace(type="apiKey")),
        requires_openai_auth=True,
    )
    install_fake_sdk(monkeypatch, produce, account_resp=account)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={"CODEX_API_KEY": "sk-x"})
    )
    sc = res.structured_content
    assert sc["logged_in"] is True
    assert sc["account"] == {"type": "api_key", "email": None, "plan_type": None}


def test_status_probe_not_authenticated(monkeypatch, tmp_path):
    async def produce(handle):
        if False:
            yield

    account = SimpleNamespace(account=None, requires_openai_auth=True)
    install_fake_sdk(monkeypatch, produce, account_resp=account)
    res = asyncio.run(
        _status_tool(probe=True, working_directory=str(tmp_path),
                     _profile="default", _variables={})
    )
    sc = res.structured_content
    assert sc["logged_in"] is False
    assert "not authenticated" in sc["message"].lower()
    assert sc["account"] is None
    assert "cremind tools coding-agents login codex" in sc["message"]


def test_status_includes_models(monkeypatch):
    from app.tools.builtin import codex_runner as r

    async def _models(variables, profile, *, force_refresh=False):
        return {
            "models": [
                {"id": "gpt-5.1-codex", "display_name": "GPT-5.1 Codex"},
                {"id": "gpt-5.1-codex-mini", "display_name": "GPT-5.1 Codex Mini"},
            ],
            "source": "host_codex_login",
            "cached": False,
        }

    monkeypatch.setattr(r, "list_models", _models)

    async def produce(handle):
        if False:
            yield

    install_fake_sdk(monkeypatch, produce)
    res = asyncio.run(_status_tool(_profile="default", _variables={}))
    sc = res.structured_content
    ids = [m["id"] for m in sc["models"]]
    assert ids == ["gpt-5.1-codex", "gpt-5.1-codex-mini"]
    assert not sc.get("models_error")
    assert "cremind tools options codex" in sc["models_hint"]
