"""Each profile has its own working directory — across the agent runtime and
the built-in tools.

Two profiles (``admin`` + ``javis``) throughout, never admin alone: a folder
that is accidentally process-global looks correct on a one-profile box. Every
surface that lets the agent point at a directory must refuse the OTHER
profile's folder, and the admin is not exempt. The ``contained`` layout is the
container one: ``CREMIND_WORKSPACES_DIR`` lives inside the admin's (legacy)
folder, so the admin reaches the others' folders by plain relative paths too.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.config import working_dirs as wd  # noqa: E402
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult  # noqa: E402
from app.utils.context_storage import clear_context, get_context, set_context  # noqa: E402

# By module path: ``app.config.settings`` the attribute is a Dynaconf object.
cfg = importlib.import_module("app.config.settings")


class _Store:
    """The three DynamicConfigStorage methods working_dirs uses."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


def _real(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


@pytest.fixture
def two(tmp_path, monkeypatch):
    home = tmp_path / "home"
    sysdir = home / ".cremind"
    sysdir.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))  # ntpath.expanduser
    monkeypatch.setenv("HOME", str(home))         # posixpath.expanduser
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"admin": None, "javis": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()
    ns = SimpleNamespace(home=home, sysdir=sysdir, store=store)
    ns.admin = Path(cfg.get_user_working_directory("admin"))
    ns.javis = Path(cfg.get_user_working_directory("javis"))
    yield ns
    wd.invalidate()


@pytest.fixture
def contained(two, monkeypatch):
    """The admin's folder holds the workspaces root (the container layout)."""
    docs = two.home / "Documents"
    docs.mkdir()
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(docs / "workspaces"))
    two.store.rows["admin"] = str(docs)
    wd.invalidate()
    two.docs = docs
    two.admin = Path(cfg.get_user_working_directory("admin"))
    two.javis = Path(cfg.get_user_working_directory("javis"))
    assert _real(two.admin) == _real(docs)
    assert _real(two.javis) == _real(docs / "workspaces" / "javis")
    return two


# ── the folder each surface resolves ────────────────────────────────────────


def test_the_system_var_is_each_profiles_own_folder(two):
    from app.config.system_vars import build_system_env

    assert _real(build_system_env("admin")["CREMIND_USER_WORKING_DIR"]) == _real(two.admin)
    assert _real(build_system_env("javis")["CREMIND_USER_WORKING_DIR"]) == _real(two.javis)
    # No profile, no folder — never somebody else's.
    assert "CREMIND_USER_WORKING_DIR" not in build_system_env(None)


class _Echo(BuiltInTool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.seen = []

    async def run(self, arguments):
        self.seen.append(dict(arguments))
        return BuiltInToolResult(structured_content={"ok": True})


def _drive(adapter, *, profile, context_id):
    async def _go():
        async for _ev in adapter.request(
            query="q", context_id=context_id, profile=profile,
            decided_calls=[{"name": "echo", "arguments": {}}],
        ):
            pass

    asyncio.run(_go())


def test_the_adapter_injects_each_profiles_own_folder(two):
    from app.tools.builtin.adapter import BuiltInToolAdapter

    tool = _Echo()
    adapter = BuiltInToolAdapter(tools=[tool], llm=object())
    _drive(adapter, profile="admin", context_id="ctx-wd-admin")
    _drive(adapter, profile="javis", context_id="ctx-wd-javis")
    assert _real(tool.seen[0]["_working_directory"]) == _real(two.admin)
    assert _real(tool.seen[1]["_working_directory"]) == _real(two.javis)


def test_the_adapter_drops_an_override_inside_another_profiles_folder(two):
    """A stale override (the admin moved a folder since) must not hand the
    tools another profile's directory — the admin included."""
    from app.tools.builtin.adapter import BuiltInToolAdapter

    tool = _Echo()
    adapter = BuiltInToolAdapter(tools=[tool], llm=object())
    own = two.javis / "sub"
    own.mkdir()
    try:
        set_context("ctx-ovr-j", "_working_directory_override", str(two.admin))
        set_context("ctx-ovr-a", "_working_directory_override", str(two.javis))
        set_context("ctx-ovr-own", "_working_directory_override", str(own))
        _drive(adapter, profile="javis", context_id="ctx-ovr-j")
        _drive(adapter, profile="admin", context_id="ctx-ovr-a")
        _drive(adapter, profile="javis", context_id="ctx-ovr-own")
    finally:
        for c in ("ctx-ovr-j", "ctx-ovr-a", "ctx-ovr-own"):
            clear_context(c)
    assert _real(tool.seen[0]["_working_directory"]) == _real(two.javis)
    assert _real(tool.seen[1]["_working_directory"]) == _real(two.admin)
    assert tool.seen[2]["_working_directory"] == str(own)


def test_the_prompt_shows_each_profiles_own_folder(two, monkeypatch):
    import app.agent.reasoning_agent as ra

    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")

    def _cwd_line(profile, context_id=None):
        agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
        agent.profile = profile
        agent.context_id = context_id
        agent._inject_reasoning_guidance = False
        return agent._build_instruction()

    assert str(two.admin) in _cwd_line("admin")
    assert str(two.javis) in _cwd_line("javis")
    assert str(two.admin) not in _cwd_line("javis")
    try:
        set_context("ctx-prompt-j", "_working_directory_override", str(two.admin / "x"))
        rendered = _cwd_line("javis", "ctx-prompt-j")
    finally:
        clear_context("ctx-prompt-j")
    assert str(two.admin) not in rendered and str(two.javis) in rendered


# ── hydrate ─────────────────────────────────────────────────────────────────


class _ConvStorage:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        self.updates.append((conversation_id, kwargs))


def test_hydrate_drops_a_persisted_cwd_in_another_profiles_folder(two):
    from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY, hydrate_working_directory

    storage = _ConvStorage({"c-j": {
        "id": "c-j", "profile": "javis", "working_directory": str(two.admin),
    }})
    try:
        got = asyncio.run(hydrate_working_directory("c-j", storage))
        assert _real(got) == _real(two.javis)
        assert storage.updates == [("c-j", {"working_directory": None})]
        assert get_context("c-j", WORKING_DIR_OVERRIDE_KEY) is None
    finally:
        clear_context("c-j")


def test_hydrate_drops_a_foreign_in_memory_override_too(two):
    from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY, hydrate_working_directory

    storage = _ConvStorage({"c-a": {"id": "c-a", "profile": "admin"}})
    try:
        set_context("c-a", WORKING_DIR_OVERRIDE_KEY, str(two.javis))
        got = asyncio.run(hydrate_working_directory("c-a", storage, profile="admin"))
        assert _real(got) == _real(two.admin)
        assert get_context("c-a", WORKING_DIR_OVERRIDE_KEY) is None
    finally:
        clear_context("c-a")


def test_hydrate_never_guesses_a_profile(two):
    from app.utils.working_directory import hydrate_working_directory

    with pytest.raises(ValueError):
        asyncio.run(hydrate_working_directory("", None))
    with pytest.raises(ValueError):
        asyncio.run(hydrate_working_directory("c-none", _ConvStorage({})))
    # The row's profile answers when the caller has none.
    got = asyncio.run(hydrate_working_directory(
        "c-row", _ConvStorage({"c-row": {"id": "c-row", "profile": "javis"}}),
    ))
    assert _real(got) == _real(two.javis)


# ── change_working_directory ───────────────────────────────────────────────


def _cwd_tool(monkeypatch, args):
    from app.tools.builtin import change_working_directory as cwd_tool
    import app.events.runner as runner

    class _Bus:
        async def publish(self, *a):
            pass

    monkeypatch.setattr(cwd_tool, "get_event_stream_bus", lambda: _Bus())
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: _ConvStorage({}))
    return asyncio.run(cwd_tool.ChangeWorkingDirectoryTool().run(args))


def test_user_working_is_the_callers_own_folder(two, monkeypatch):
    try:
        res = _cwd_tool(monkeypatch, {
            "target": "user_working", "_context_id": "cwd-uw", "_profile": "javis",
        })
    finally:
        clear_context("cwd-uw")
    assert _real(res.structured_content["current"]) == _real(two.javis)


@pytest.mark.parametrize("caller,other", [("javis", "admin"), ("admin", "javis")])
def test_custom_refuses_another_profiles_folder(two, monkeypatch, caller, other):
    target = getattr(two, other) / "deep"
    target.mkdir()
    try:
        res = _cwd_tool(monkeypatch, {
            "target": "custom", "path": str(target),
            "_context_id": f"cwd-{caller}", "_profile": caller,
        })
        assert "That folder belongs to another profile." in res.content[0]["text"]
        assert get_context(f"cwd-{caller}", "_working_directory_override") is None
    finally:
        clear_context(f"cwd-{caller}")


def test_custom_into_an_own_or_ordinary_folder_still_works(two, monkeypatch):
    own = two.javis / "proj"
    own.mkdir()
    ordinary = two.home / "Projects"
    ordinary.mkdir()
    try:
        for i, target in enumerate((own, ordinary)):
            res = _cwd_tool(monkeypatch, {
                "target": "custom", "path": str(target),
                "_context_id": f"cwd-ok-{i}", "_profile": "javis",
            })
            assert res.structured_content["current"] == str(target.resolve())
    finally:
        clear_context("cwd-ok-0")
        clear_context("cwd-ok-1")


def test_change_working_directory_needs_a_profile(two, monkeypatch):
    res = _cwd_tool(monkeypatch, {"target": "user_working", "_context_id": "cwd-np"})
    assert "missing profile" in res.content[0]["text"]


# ── sandbox auto-recovery ──────────────────────────────────────────────────


def _denial(path):
    return {
        "error": "Access denied",
        "message": f"Access denied: '{path}' resolves outside the allowed directories.",
    }


def test_sandbox_recovery_never_lands_in_another_profiles_folder(two):
    from app.tools.builtin.adapter import resolve_sandbox_recovery_dir

    admin_sub = two.admin / "reports"
    admin_sub.mkdir()
    javis_sub = two.javis / "reports"
    javis_sub.mkdir()
    ordinary = two.home / "Sources" / "calc"
    ordinary.mkdir(parents=True)

    # Both under home, both existing — only the profile decides.
    assert resolve_sandbox_recovery_dir(
        _denial(admin_sub), {"path": str(admin_sub), "_profile": "javis"},
    ) is None
    assert resolve_sandbox_recovery_dir(
        _denial(javis_sub), {"path": str(javis_sub)}, "admin",
    ) is None
    assert resolve_sandbox_recovery_dir(
        _denial(ordinary), {"path": str(ordinary), "_profile": "javis"},
    ) == os.path.realpath(str(ordinary))


# ── coding agents ──────────────────────────────────────────────────────────


def test_coding_agent_cwd_is_the_own_folder_and_never_another(two):
    from app.utils.working_directory import resolve_tool_cwd

    cwd, err = resolve_tool_cwd({"_profile": "javis"})
    assert err is None and _real(cwd) == _real(two.javis)
    for caller, other in (("javis", two.admin), ("admin", two.javis)):
        cwd, err = resolve_tool_cwd({"_profile": caller, "working_directory": str(other)})
        assert cwd is None and "belongs to another profile" in err
        cwd, err = resolve_tool_cwd({"_profile": caller, "_working_directory": str(other)})
        assert cwd is None
    # Nothing names a folder and nobody is calling: no guess.
    cwd, err = resolve_tool_cwd({})
    assert cwd is None and err


@pytest.mark.parametrize("module", ["claude_code", "codex"])
def test_coding_agent_run_refuses_another_profiles_folder(two, monkeypatch, module):
    mod = importlib.import_module(f"app.tools.builtin.{module}")
    started = []

    async def _never(**kwargs):  # pragma: no cover - must not run
        started.append(kwargs)
        raise AssertionError("started a task in another profile's folder")

    monkeypatch.setattr(mod, "load_sdk", lambda: (object(), None))
    monkeypatch.setattr(mod, "start_task", _never)
    tool_cls = next(
        getattr(mod, n) for n in dir(mod)
        if n.endswith("RunTool") and isinstance(getattr(mod, n), type)
    )
    res = asyncio.run(tool_cls().run({
        "prompt": "do it", "working_directory": str(two.javis),
        "_profile": "admin", "_context_id": "c", "_variables": {},
    }))
    assert res.structured_content["error"] == "WorkingDirectoryError"
    assert "belongs to another profile" in res.structured_content["message"]
    assert started == []


# ── file watcher tool ──────────────────────────────────────────────────────


def _watch(args):
    from app.tools.builtin.register_file_watcher import RegisterFileWatcherTool

    return asyncio.run(RegisterFileWatcherTool().run({"action": "report it", **args}))


def test_watcher_relative_paths_join_the_callers_own_folder(two):
    from app.tools.builtin.register_file_watcher import _resolve_path

    path, rel = _resolve_path("inbox", "javis")
    assert rel and _real(path) == _real(two.javis / "inbox")
    path, rel = _resolve_path(None, "admin")
    assert rel and _real(path) == _real(two.admin)


@pytest.mark.parametrize("caller,other", [("javis", "admin"), ("admin", "javis")])
def test_watcher_refuses_another_profiles_folder(two, caller, other):
    res = _watch({"_profile": caller, "_context_id": "c", "path": str(getattr(two, other))})
    assert "belongs to another profile" in res.content[0]["text"]


def test_watcher_refuses_a_relative_path_into_another_workspace(contained):
    res = _watch({"_profile": "admin", "_context_id": "c", "path": "workspaces/javis"})
    assert "belongs to another profile" in res.content[0]["text"]


# ── system_file ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller,other", [("javis", "admin"), ("admin", "javis")])
def test_safe_resolve_refuses_another_profiles_folder(two, caller, other):
    from app.tools.builtin.system_file import _safe_resolve

    target = getattr(two, other) / "secret.txt"
    with pytest.raises(ValueError, match="another profile"):
        _safe_resolve(str(getattr(two, caller)), str(target), [str(two.home)], profile=caller)
    # Without a profile the caller is judged by its working directory.
    with pytest.raises(ValueError, match="another profile"):
        _safe_resolve(str(getattr(two, caller)), str(target), [str(two.home)])
    own = _safe_resolve(str(getattr(two, caller)), "notes.txt", profile=caller)
    assert _real(own) == _real(getattr(two, caller) / "notes.txt")
    assert _real(_safe_resolve(str(getattr(two, caller)), "notes.txt")) == _real(own)


def test_safe_resolve_refuses_a_relative_path_into_another_workspace(contained):
    from app.tools.builtin.system_file import _safe_resolve

    base = str(contained.admin)
    with pytest.raises(ValueError, match="another profile"):
        _safe_resolve(base, "workspaces/javis/x.txt", profile="admin")
    assert _safe_resolve(base, "workspaces/admin/x.txt", profile="admin")
    assert _safe_resolve(base, "notes.txt", profile="admin")


def test_search_and_list_hide_the_other_profiles_workspace(contained):
    from app.tools.builtin.system_file import ListFilesTool, SearchFilesTool

    docs = contained.docs
    (docs / "note-admin.txt").write_text("a")
    (docs / "workspaces" / "admin").mkdir(parents=True, exist_ok=True)
    (docs / "workspaces" / "admin" / "note-own.txt").write_text("b")
    (contained.javis / "note-secret.txt").write_text("c")

    found = asyncio.run(SearchFilesTool(str(docs)).run({
        "query": "note", "_working_directory": str(docs), "_profile": "admin",
    })).structured_content
    names = {r["name"] for r in found["results"]}
    assert names == {"note-admin.txt", "note-own.txt"}

    listed = asyncio.run(ListFilesTool(str(docs)).run({
        "path": "workspaces", "_working_directory": str(docs), "_profile": "admin",
    })).structured_content
    assert {e["name"] for e in listed["entries"]} == {"admin"}


def test_moving_a_folder_that_holds_another_workspace_is_refused(contained):
    from app.tools.builtin.system_file import MoveFileTool

    docs = contained.docs
    res = asyncio.run(MoveFileTool(str(docs)).run({
        "source_path": "workspaces", "destination_path": "elsewhere",
        "_working_directory": str(docs), "_profile": "admin",
    })).structured_content
    assert res["error"] == "Access denied"
    assert "other profiles' working directories" in res["message"]
    assert (docs / "workspaces" / "javis").is_dir()


# ── channel attachments ────────────────────────────────────────────────────


def test_outbound_attachments_never_ship_another_profiles_file(contained):
    from app.channels.attachments import validate_outbound_paths

    theirs = contained.javis / "payroll.pdf"
    theirs.write_bytes(b"%PDF")
    mine = contained.docs / "report.pdf"
    mine.write_bytes(b"%PDF")

    ok, rejected = validate_outbound_paths("admin", [str(theirs), str(mine)])
    assert [Path(o["path"]).name for o in ok] == ["report.pdf"]
    assert rejected == [{"path": str(theirs), "reason": "inside another profile's working directory"}]

    # A turn cwd inside someone else's folder is no extra root either.
    ok, rejected = validate_outbound_paths(
        "javis", [str(mine)], extra_roots=[str(contained.docs)],
    )
    assert ok == [] and len(rejected) == 1
