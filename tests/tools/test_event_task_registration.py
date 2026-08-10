"""Registering one-shot event tasks through the three agent-facing surfaces.

Schemas are always exposed (they are part of the cached tools prefix), so all
the task rules are enforced when the tool actually runs. That makes these the
tests that keep a malformed call from half-registering something.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import app.tools.builtin.register_skill_event as RSE
from app.events.task_policy import TASK_TIMEOUT_DEFAULT_MINUTES


class _FakeStore:
    def __init__(self):
        self.inserted = []

    def insert(self, **kwargs):
        self.inserted.append(kwargs)
        return {"id": f"sub{len(self.inserted)}", **kwargs}


class _FakeConvStorage:
    async def get_or_create_conversation(self, *, profile, context_id):
        return {"id": "conv1"}


def _register(monkeypatch, *, triggers=("issue_created",), **kwargs):
    store = _FakeStore()
    monkeypatch.setattr(RSE, "get_event_subscription_storage", lambda: store)
    monkeypatch.setattr("app.storage.get_conversation_storage", lambda: _FakeConvStorage())
    monkeypatch.setattr(
        RSE, "_read_events_metadata",
        lambda src: [{"name": "issue_created"}, {"name": "issue_closed"}],
    )

    async def _accept(**kw):
        return None
    monkeypatch.setattr("app.events.action_check.gate_registration_action", _accept)

    out = asyncio.run(RSE.register_skill_events(
        profile="admin", context_id="ctx", skill_id="admin__jira",
        skill_source="/skills/jira", triggers=list(triggers),
        action="report the issue key and its status",
        request_context="", **kwargs,
    ))
    return out, store


# ── skill subscribe ─────────────────────────────────────────────────────────


def test_task_subscription_is_persisted_with_a_deadline(monkeypatch):
    out, store = _register(monkeypatch, task=True, timeout_minutes=120)
    assert len(store.inserted) == 1
    row = store.inserted[0]
    assert row["task"] is True
    assert row["timeout_at"] == pytest.approx(time.time() + 7200, abs=5)
    # The result text carries the behavioural contract, not just a confirmation.
    assert "one-shot TASK" in out
    assert "END YOUR TURN" in out
    assert "Do not sleep, poll, or re-check" in out


def test_task_without_an_explicit_timeout_still_gets_one(monkeypatch):
    _, store = _register(monkeypatch, task=True)
    assert store.inserted[0]["timeout_at"] == pytest.approx(
        time.time() + TASK_TIMEOUT_DEFAULT_MINUTES * 60, abs=5,
    )


def test_standing_subscription_is_unchanged(monkeypatch):
    """The pre-feature path must not shift — same row, same confirmation shape."""
    out, store = _register(monkeypatch)
    row = store.inserted[0]
    assert row["task"] is False
    assert row["timeout_at"] is None
    assert "Subscribed this conversation to the 'issue_created' event" in out
    assert "one-shot" not in out


def test_a_task_may_await_only_one_event(monkeypatch):
    """N rows for one wait would inject N continuations and strand the losers."""
    out, store = _register(
        monkeypatch, triggers=("issue_created", "issue_closed"), task=True,
    )
    assert store.inserted == []
    assert "exactly one `trigger`" in out
    assert "one `subscribe` call per event" in out


def test_multi_trigger_standing_subscription_still_fans_out(monkeypatch):
    _, store = _register(monkeypatch, triggers=("issue_created", "issue_closed"))
    assert len(store.inserted) == 2


def test_timeout_without_task_registers_nothing(monkeypatch):
    out, store = _register(monkeypatch, timeout_minutes=30)
    assert store.inserted == []
    assert "only applies to a one-shot task" in out


def test_out_of_range_timeout_registers_nothing(monkeypatch):
    out, store = _register(monkeypatch, task=True, timeout_minutes=0)
    assert store.inserted == []
    assert "must be a whole number of minutes" in out


def test_the_gate_is_told_this_is_a_task(monkeypatch):
    """A task action reports back automatically; the checker must not demand it."""
    seen = {}

    async def _capture(**kw):
        seen.update(kw)
        return None

    store = _FakeStore()
    monkeypatch.setattr(RSE, "get_event_subscription_storage", lambda: store)
    monkeypatch.setattr("app.storage.get_conversation_storage", lambda: _FakeConvStorage())
    monkeypatch.setattr(RSE, "_read_events_metadata", lambda src: [{"name": "issue_created"}])
    monkeypatch.setattr("app.events.action_check.gate_registration_action", _capture)

    asyncio.run(RSE.register_skill_events(
        profile="admin", context_id="ctx", skill_id="admin__jira",
        skill_source="/skills/jira", triggers=["issue_created"],
        action="report the outcome", request_context="", task=True,
    ))
    assert seen.get("task") is True


# ── file watcher ────────────────────────────────────────────────────────────


def _watcher_schema():
    from app.tools.builtin.register_file_watcher import RegisterFileWatcherTool
    return RegisterFileWatcherTool.parameters


def test_watcher_schema_exposes_the_task_fields(monkeypatch):
    schema = _watcher_schema()
    props = schema["properties"]
    assert props["task"]["type"] == "boolean"
    assert props["timeout_minutes"]["type"] == "integer"
    # Unchanged contract: still only `action` is required, still closed.
    assert schema["required"] == ["action"]
    assert schema["additionalProperties"] is False
    # Must survive the wire as-is (it rides the cached tools prefix).
    assert json.loads(json.dumps(schema)) == schema


def test_subscribe_schema_exposes_the_task_fields():
    from app.agent.reasoning_agent import ReasoningAgent

    agent = ReasoningAgent.__new__(ReasoningAgent)
    spec = agent._skill_subscribe_spec([{"name": "new_email", "description": "mail"}])
    props = spec["properties"]
    assert props["task"]["type"] == "boolean"
    assert props["timeout_minutes"]["type"] == "integer"
    assert spec["required"] == ["trigger", "action"]
    assert spec["additionalProperties"] is False
    # Byte-stable across builds — a varying tools block would bust the cache.
    again = agent._skill_subscribe_spec([{"name": "new_email", "description": "mail"}])
    assert json.dumps(spec, sort_keys=False) == json.dumps(again, sort_keys=False)
