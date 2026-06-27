"""Tests for the compact_conversation tool (model-driven compaction)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

from app.tools.builtin.compact_conversation import CompactConversationTool  # noqa: E402


def test_compact_tool_requires_summary() -> None:
    res = asyncio.run(CompactConversationTool().run({"summary": "", "_context_id": "ctx"}))
    assert res.structured_content["error"] == "EmptySummary"


def test_compact_tool_requires_context() -> None:
    res = asyncio.run(CompactConversationTool().run({"summary": "S"}))
    assert res.structured_content["error"] == "NoContext"


def test_compact_tool_delegates_to_apply_compaction(monkeypatch) -> None:
    captured: dict = {}

    class _FakeStorage:
        async def get_conversation_by_context(self, profile, context_id):
            return {"id": "conv1"}

        async def get_conversation(self, cid):
            return {"id": cid}

    import app.events.runner as runner
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: _FakeStorage())

    import app.agent.compaction as compaction

    async def _fake_apply(*, conversation_id, profile, summary, long_term, conversation_storage):
        captured.update(
            conversation_id=conversation_id, profile=profile,
            summary=summary, long_term=long_term,
        )
        return {"watermark": 7, "summary_chars": len(summary), "long_term_stored": len(long_term)}

    monkeypatch.setattr(compaction, "apply_compaction", _fake_apply)

    res = asyncio.run(CompactConversationTool().run({
        "summary": "THE SUMMARY",
        "long_term_memories": ["User is Lee"],
        "_context_id": "ctx-1",
        "_profile": "admin",
    }))

    assert captured["conversation_id"] == "conv1"
    assert captured["profile"] == "admin"
    assert captured["summary"] == "THE SUMMARY"
    assert captured["long_term"] == ["User is Lee"]
    assert res.structured_content["status"] == "compacted"
    assert res.structured_content["watermark"] == 7
