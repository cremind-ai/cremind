"""Unit tests for the generic agent-activity feed's snapshot shape.

Focus: the live context-usage payload added by :meth:`AgentActivity.update_usage`
surfaces in :meth:`AgentActivity.snapshot` (which every consumer — SSE publish,
message-metadata stamping, restore, REST — passes through verbatim).

Coroutines are driven with ``asyncio.run`` (matching the repo's other tests).
``_schedule_flush`` is neutralised so mutations don't spin up the real coalescing
timer / stream-bus singleton across back-to-back ``asyncio.run`` loops.
"""

from __future__ import annotations

import asyncio


def _activity(monkeypatch):
    from app.agent.agent_activity import AgentActivity

    monkeypatch.setattr(AgentActivity, "_schedule_flush", lambda self: None)
    return AgentActivity(
        conversation_id="conv-1",
        profile="default",
        agent="claude_code",
        task_id="t1",
        title="do a thing",
    )


def test_snapshot_includes_usage_key_before_any_update(monkeypatch):
    act = _activity(monkeypatch)
    snap = act.snapshot()
    # The key is always present (None until the first update) so the UI type is stable.
    assert "usage" in snap
    assert snap["usage"] is None


def test_update_usage_sets_payload_and_bumps_updated_at(monkeypatch):
    act = _activity(monkeypatch)
    before = act.updated_at

    async def body():
        await act.update_usage({"context_tokens": 121_400, "context_window": 1_000_000})

    asyncio.run(body())

    assert act.usage == {"context_tokens": 121_400, "context_window": 1_000_000}
    assert act.snapshot()["usage"] == {"context_tokens": 121_400, "context_window": 1_000_000}
    assert act.updated_at >= before
