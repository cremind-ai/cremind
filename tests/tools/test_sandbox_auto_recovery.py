"""Tests for the system_file sandbox auto-recovery.

When a ``system_file`` op denies an absolute path that resolves outside the
allowed roots but names an existing location under the user's home, the adapter
switches the conversation working directory there and retries once. These cover
the two pieces that don't need the adapter's LLM:

* ``adapter.resolve_sandbox_recovery_dir`` — the pure decision helper (does the
  denial name a recoverable directory under home?).
* ``working_directory.switch_conversation_cwd`` — the shared in-memory + durable
  cwd-switch used by the recovery path and by ``change_working_directory``.
"""

import asyncio
import os

import pytest

from app.tools.builtin.adapter import resolve_sandbox_recovery_dir
from app.tools.builtin.system_file import _safe_resolve
from app.utils.context_storage import clear_context, get_context
from app.utils.working_directory import (
    WORKING_DIR_OVERRIDE_KEY,
    switch_conversation_cwd,
)


def _set_home(monkeypatch, home):
    """Point ``os.path.expanduser('~')`` at *home* on both Windows and POSIX."""
    monkeypatch.setenv("USERPROFILE", str(home))  # ntpath.expanduser
    monkeypatch.setenv("HOME", str(home))         # posixpath.expanduser


def _denial(path):
    """A denial result_data shaped exactly like ``system_file`` returns."""
    return {
        "error": "Access denied",
        "message": (
            f"Access denied: '{path}' resolves outside the allowed directories. "
            f"Allowed roots: ['a', 'b']. Absolute paths are accepted only under "
            f"one of these."
        ),
    }


# --------------------------------------------------------------------------- #
# resolve_sandbox_recovery_dir
# --------------------------------------------------------------------------- #

def test_recovery_existing_dir_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = home / "Sources" / "mcp-calculator"
    target.mkdir(parents=True)
    _set_home(monkeypatch, home)
    got = resolve_sandbox_recovery_dir(_denial(str(target)), {"path": str(target)})
    assert got == os.path.realpath(str(target))


def test_recovery_existing_file_returns_parent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = home / "proj"
    d.mkdir(parents=True)
    f = d / "main.py"
    f.write_text("x")
    _set_home(monkeypatch, home)
    got = resolve_sandbox_recovery_dir(_denial(str(f)), {"path": str(f)})
    assert got == os.path.realpath(str(d))


def test_recovery_new_file_in_existing_dir_returns_parent(tmp_path, monkeypatch):
    # Writing a not-yet-existent file into an existing folder must recover to the
    # folder so the write resolves under the new cwd.
    home = tmp_path / "home"
    d = home / "proj"
    d.mkdir(parents=True)
    f = d / "new.txt"  # does not exist yet
    _set_home(monkeypatch, home)
    got = resolve_sandbox_recovery_dir(_denial(str(f)), {"path": str(f)})
    assert got == os.path.realpath(str(d))


def test_recovery_none_for_nonexistent_path(tmp_path, monkeypatch):
    # Parent dir also missing (a typo) → no recovery, denial stands.
    home = tmp_path / "home"
    home.mkdir()
    missing = home / "nope" / "gone.txt"
    _set_home(monkeypatch, home)
    assert resolve_sandbox_recovery_dir(_denial(str(missing)), {"path": str(missing)}) is None


def test_recovery_none_for_dir_outside_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "elsewhere"  # sibling of home, not under it
    outside.mkdir()
    _set_home(monkeypatch, home)
    assert resolve_sandbox_recovery_dir(_denial(str(outside)), {"path": str(outside)}) is None


def test_recovery_none_for_non_denial_results(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = home / "d"
    d.mkdir(parents=True)
    _set_home(monkeypatch, home)
    args = {"path": str(d)}
    assert resolve_sandbox_recovery_dir({"error": "Not found", "message": "x"}, args) is None
    assert resolve_sandbox_recovery_dir({"error": "Not a directory", "message": "x"}, args) is None
    assert resolve_sandbox_recovery_dir({"entries": []}, args) is None
    assert resolve_sandbox_recovery_dir("not-a-dict", args) is None


def test_recovery_falls_back_to_tool_args_path(tmp_path, monkeypatch):
    # Message without a quoted path → use the tool's path-style arguments.
    home = tmp_path / "home"
    d = home / "proj"
    d.mkdir(parents=True)
    _set_home(monkeypatch, home)
    result = {"error": "Access denied",
              "message": "resolves outside the allowed directories"}
    assert resolve_sandbox_recovery_dir(result, {"path": str(d)}) == os.path.realpath(str(d))
    # move/copy use source_path/destination_path.
    assert resolve_sandbox_recovery_dir(
        result, {"source_path": str(d)}) == os.path.realpath(str(d))


def test_recovery_ignores_relative_path(tmp_path, monkeypatch):
    # A relative arg is a normal in-cwd path, not an out-of-sandbox reference.
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    _set_home(monkeypatch, home)
    result = {"error": "Access denied",
              "message": "resolves outside the allowed directories"}
    assert resolve_sandbox_recovery_dir(result, {"path": "proj/sub"}) is None


# --------------------------------------------------------------------------- #
# Integration: a real _safe_resolve denial message is recoverable
# --------------------------------------------------------------------------- #

def test_real_safe_resolve_message_is_recoverable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = home / "Documents"
    base.mkdir(parents=True)
    target = home / "Sources" / "mcp-calculator"
    target.mkdir(parents=True)
    _set_home(monkeypatch, home)

    with pytest.raises(ValueError) as exc:
        _safe_resolve(str(base), str(target))
    result = {"error": "Access denied", "message": str(exc.value)}
    assert resolve_sandbox_recovery_dir(result, {"path": str(target)}) == os.path.realpath(str(target))


# --------------------------------------------------------------------------- #
# switch_conversation_cwd
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# End-to-end: BuiltInToolAdapter.request drives detect → switch → retry
# --------------------------------------------------------------------------- #

def test_adapter_request_auto_recovers_end_to_end(tmp_path, monkeypatch):
    from app.tools.builtin import adapter as adapter_mod
    from app.tools.builtin.adapter import BuiltInToolAdapter
    from app.tools.builtin.system_file import ListFilesTool
    from app.events import runner as runner_mod

    home = tmp_path / "home"
    documents = home / "Documents"
    documents.mkdir(parents=True)
    target = home / "Sources" / "mcp-calculator"
    target.mkdir(parents=True)
    (target / "server.py").write_text("print('hi')")

    _set_home(monkeypatch, home)
    # First-run cwd is a controlled temp Documents (not the target), so the
    # out-of-sandbox target is denied before recovery kicks in.
    monkeypatch.setattr(adapter_mod, "get_user_working_directory", lambda: str(documents))

    class FakeConvStorage:
        def __init__(self):
            self.calls = []

        async def update_conversation(self, conversation_id, **kwargs):
            self.calls.append((conversation_id, kwargs))

    storage = FakeConvStorage()
    monkeypatch.setattr(runner_mod, "get_conversation_storage", lambda: storage)

    adapter = BuiltInToolAdapter(tools=[ListFilesTool(str(documents))], llm=object())
    cid = "test-adapter-recovery"

    async def _drive():
        async for _ev in adapter.request(
            query="list it",
            context_id=cid,
            profile="admin",
            decided_calls=[{"name": "list_files", "arguments": {"path": str(target)}}],
        ):
            pass

    try:
        asyncio.run(_drive())
        # Recovery switched the conversation cwd to the target folder and persisted it.
        assert get_context(cid, WORKING_DIR_OVERRIDE_KEY) == os.path.realpath(str(target))
        assert storage.calls
        assert storage.calls[-1][1] == {"working_directory": os.path.realpath(str(target))}
    finally:
        clear_context(cid, WORKING_DIR_OVERRIDE_KEY)


def test_switch_conversation_cwd_sets_and_persists(tmp_path):
    cid = "test-conv-switch-cwd"
    d = tmp_path / "target"
    d.mkdir()

    class FakeConvStorage:
        def __init__(self):
            self.calls = []

        async def update_conversation(self, conversation_id, **kwargs):
            self.calls.append((conversation_id, kwargs))

    storage = FakeConvStorage()
    try:
        asyncio.run(switch_conversation_cwd(cid, str(d), storage, publish=False))
        assert get_context(cid, WORKING_DIR_OVERRIDE_KEY) == str(d)
        assert storage.calls == [(cid, {"working_directory": str(d)})]
    finally:
        clear_context(cid, WORKING_DIR_OVERRIDE_KEY)
