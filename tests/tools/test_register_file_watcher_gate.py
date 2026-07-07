"""RegisterFileWatcherTool.run() applies the self-containment gate before it
persists a watcher subscription. Reject → error + no insert; pass → inserted +
armed; no LLM wired → fail-open → inserted.
"""

from __future__ import annotations

import asyncio

import app.events.action_check as ac
import app.tools.builtin.register_file_watcher as RFW
from app.events.action_check import ActionCheckResult


class _FakeStore:
    def __init__(self):
        self.inserted = None

    def insert(self, **kwargs):
        self.inserted = kwargs
        return {"id": "fw1", **kwargs}


class _FakeManager:
    def arm(self, row):
        return True


class _FakeConvStorage:
    async def get_or_create_conversation(self, *, profile, context_id):
        return {"id": "conv1"}


def _drive(arguments, monkeypatch, *, store=None, gate=None):
    store = store or _FakeStore()
    monkeypatch.setattr(RFW, "get_file_watcher_storage", lambda: store)
    monkeypatch.setattr(RFW, "get_file_watcher_manager", lambda: _FakeManager())
    monkeypatch.setattr("app.storage.get_conversation_storage", lambda: _FakeConvStorage())
    if gate is not None:
        monkeypatch.setattr("app.events.action_check.gate_registration_action", gate)
    args = {"_profile": "p", "_context_id": "ctx", **arguments}
    result = asyncio.run(RFW.RegisterFileWatcherTool().run(args))
    return result, store


def _text(result):
    return " ".join(
        p.get("text", "") for p in (result.content or []) if isinstance(p, dict)
    )


def test_gate_reject_blocks_insert(monkeypatch, tmp_path):
    async def _reject(**kw):
        return ActionCheckResult(False, ["'the file above' — inline the full path"], "path missing")

    result, store = _drive(
        {"path": str(tmp_path), "action": "Process the file mentioned above.",
         "triggers": ["created"]},
        monkeypatch, gate=_reject,
    )
    assert "not self-contained" in _text(result)
    assert store.inserted is None  # nothing persisted


def test_gate_pass_inserts(monkeypatch, tmp_path):
    async def _accept(**kw):
        return None

    result, store = _drive(
        {"path": str(tmp_path), "action": "Move new files to C:/archive and log the name.",
         "triggers": ["created"]},
        monkeypatch, gate=_accept,
    )
    assert store.inserted is not None
    assert store.inserted["action"].startswith("Move new files")


def test_gate_fail_open_when_no_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: None)
    monkeypatch.setattr(ac, "record_action_check_usage", _noop)
    result, store = _drive(
        {"path": str(tmp_path), "action": "Process the file above.", "triggers": ["created"]},
        monkeypatch,  # real gate, no agent → fail open
    )
    assert store.inserted is not None


async def _noop(**kw):
    return None
