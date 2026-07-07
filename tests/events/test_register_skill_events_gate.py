"""register_skill_events() applies the self-containment gate before persisting a
subscription. Reject → rejection string + no insert; pass → inserted; no LLM
wired → fail-open → inserted.
"""

from __future__ import annotations

import asyncio

import app.events.action_check as ac
import app.tools.builtin.register_skill_event as RSE
from app.events.action_check import ActionCheckResult


class _FakeStore:
    def __init__(self):
        self.inserted = []

    def insert(self, **kwargs):
        self.inserted.append(kwargs)
        return {"id": f"sub{len(self.inserted)}", **kwargs}


class _FakeConvStorage:
    async def get_or_create_conversation(self, *, profile, context_id):
        return {"id": "conv1"}


def _drive(monkeypatch, *, action, gate=None, store=None, request_context=""):
    store = store or _FakeStore()
    monkeypatch.setattr(RSE, "get_event_subscription_storage", lambda: store)
    monkeypatch.setattr("app.storage.get_conversation_storage", lambda: _FakeConvStorage())
    # Skill declares one event; skip real SKILL.md metadata parsing.
    monkeypatch.setattr(RSE, "_read_events_metadata", lambda src: [{"name": "issue_created"}])
    if gate is not None:
        monkeypatch.setattr("app.events.action_check.gate_registration_action", gate)
    out = asyncio.run(RSE.register_skill_events(
        profile="admin", context_id="ctx", skill_id="admin__jira",
        skill_source="/skills/jira", triggers=["issue_created"],
        action=action, request_context=request_context,
    ))
    return out, store


def test_gate_reject_blocks_insert(monkeypatch):
    async def _reject(**kw):
        return ActionCheckResult(False, ["'the issue above' — inline the issue key"], "key missing")

    out, store = _drive(monkeypatch, action="Summarize the issue mentioned above.", gate=_reject)
    assert "not self-contained" in out
    assert store.inserted == []  # nothing persisted


def test_gate_pass_inserts(monkeypatch):
    async def _accept(**kw):
        return None

    out, store = _drive(
        monkeypatch,
        action="Email li@olli-ai.com a summary of the new Jira issue with its key and title.",
        gate=_accept,
    )
    assert len(store.inserted) == 1
    assert store.inserted[0]["event_type"] == "issue_created"
    assert "Subscribed this conversation" in out


def test_gate_passes_request_context(monkeypatch):
    seen = {}

    async def _capture(**kw):
        seen.update(kw)
        return None

    _drive(monkeypatch, action="do the thing", gate=_capture,
           request_context="the original ask with https://x.example/i")
    assert seen.get("request_context") == "the original ask with https://x.example/i"
    assert seen.get("tool_name") == "this skill's subscribe"


def test_gate_fail_open_when_no_agent(monkeypatch):
    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: None)
    monkeypatch.setattr(ac, "record_action_check_usage", _noop)
    out, store = _drive(monkeypatch, action="Summarize the issue above.")  # real gate, no agent
    assert len(store.inserted) == 1


async def _noop(**kw):
    return None
