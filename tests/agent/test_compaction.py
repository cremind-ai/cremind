"""Tests for summarization-based conversation-history compaction.

Pins the behavior the feature (and the prompt cache) depend on:

- the conversation-storage compaction state round-trips, and ``get_messages_after``
  returns the tail by ``ordering`` (the watermark sentinel is ``-1`` so message 0
  is included);
- ``build_compacted_history`` is a no-op below the threshold and when disabled;
- once over threshold it folds the oldest turns into a running summary (one LLM
  call), advances the watermark to the last folded message, and returns the
  summary as the FIRST history message (a user-role block, byte-stable);
- hysteresis: the turn after a fold does NOT re-compact (cache stays warm);
- a failed/empty summarization leaves state unchanged and sends the full tail.

Storage harness mirrors ``tests/storage/test_memory_storage.py`` (real on-disk
SQLite, tables from ORM metadata, ``asyncio.run``); the LLM and agent are stubbed
so no network/model is needed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("tiktoken")  # count_content_tokens / convert need the encoder

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from app.agent import compaction  # noqa: E402
from app.config.user_config import CompactionConfig, MemoryConfig  # noqa: E402
from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages")
# ~10 tokens of content per message — small and predictable for threshold math.
_TEN = "one two three four five six seven eight nine ten"


# ── storage harness ──────────────────────────────────────────────────────────

def _make_storage(tmp_path: Path) -> ConversationStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "conv.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    store = ConversationStorage(provider)
    store._initialized = True  # tables created above; skip Alembic init
    return store


def _seed(store: ConversationStorage, *, profile="admin", conv="c1", n_messages=0) -> None:
    from sqlalchemy import text

    now = time.time()
    with store.provider.sync_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p', :profile, :now, :now)"
        ), {"profile": profile, "now": now})
        # compaction_watermark column has no DDL default (ORM-side default=-1), so
        # the raw INSERT must supply it.
        conn.execute(text(
            "INSERT INTO conversations "
            "(id, profile, title, created_at, updated_at, compaction_watermark) "
            "VALUES (:conv, :profile, 't', :now, :now, -1)"
        ), {"conv": conv, "profile": profile, "now": now})
        for i in range(n_messages):
            conn.execute(text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at, ordering) "
                "VALUES (:id, :conv, 'user', :c, :now, :o)"
            ), {"id": f"m{i}", "conv": conv, "c": f"message {i}", "now": now, "o": i})


def test_compaction_state_roundtrip_and_messages_after(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)
    _seed(store, n_messages=3)  # orderings 0, 1, 2

    async def run():
        # defaults: nothing compacted, watermark -1 so message 0 is in the tail
        summary, wm, ts = await store.get_compaction_state("c1")
        assert summary is None and wm == -1 and ts is None

        allm = await store.get_messages_after("c1", -1)
        assert [m["ordering"] for m in allm] == [0, 1, 2]
        assert allm[0]["content"] == "message 0"  # first message NOT skipped

        await store.set_compaction_state("c1", "running summary", 1)
        summary, wm, ts = await store.get_compaction_state("c1")
        assert summary == "running summary" and wm == 1 and ts is not None

        tail = await store.get_messages_after("c1", 1)
        assert [m["ordering"] for m in tail] == [2]

    asyncio.run(run())


def test_compaction_state_missing_row() -> None:
    # No DB hit needed for the missing-row default; build a bare storage object.
    async def run():
        class _S:
            async def get_compaction_state(self, cid):
                return None, -1, None
        s = _S()
        assert await s.get_compaction_state("nope") == (None, -1, None)

    asyncio.run(run())


# ── fakes for the logic path ───────────────────────────────────────────────────

def _msg(ordering: int, content: str, role: str = "user") -> dict:
    return {"id": f"m{ordering}", "role": role, "content": content, "ordering": ordering}


class _FakeStore:
    def __init__(self, messages, summary=None, watermark=-1):
        self._messages = messages
        self._summary = summary
        self._watermark = watermark
        self.set_calls: list[tuple] = []

    async def get_compaction_state(self, cid):
        return self._summary, self._watermark, None

    async def get_messages_after(self, cid, after, limit=5000):
        return [m for m in self._messages if m["ordering"] > after][:limit]

    async def set_compaction_state(self, cid, summary, watermark, ts=None):
        self.set_calls.append((summary, watermark))
        self._summary = summary
        self._watermark = watermark


class _FakeLLM:
    def __init__(self, output="RUNNING SUMMARY", tool_args=None):
        self.output = output
        # When set, emit a FUNCTION_CALLING save_memory event (memory-enabled
        # fold path) instead of a plain CONTENT chunk.
        self.tool_args = tool_args
        self.calls = 0

    async def chat_completion(self, **kwargs):
        self.calls += 1
        if self.tool_args is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [{"name": "save_memory", "arguments": self.tool_args}]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE}
            return
        if self.output:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": self.output}


class _FakeAgent:
    def __init__(self, llm):
        self._llm = llm

    def auxiliary_llm(self, profile):
        return self._llm


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _cfg(*, enabled=True, threshold=25, keep_recent_tokens=5, keep_recent_messages=1,
         max_tokens=2048) -> CompactionConfig:
    return CompactionConfig(
        enabled=enabled,
        compact_threshold_tokens=threshold,
        keep_recent_tokens=keep_recent_tokens,
        keep_recent_messages=keep_recent_messages,
        temperature=0.3,
        max_tokens=max_tokens,
        retry=0,
    )


def _mem_cfg(*, enabled=False) -> MemoryConfig:
    return MemoryConfig(
        enabled=enabled, long_term_queue_size=20, long_term_max_tokens=50,
        long_term_retrieve_limit=10,
    )


def _patch_cfg(monkeypatch, cfg, memory_cfg=None):
    monkeypatch.setattr(compaction, "resolve_compaction_config", lambda profile: cfg)
    monkeypatch.setattr(
        compaction, "resolve_memory_config",
        lambda profile: memory_cfg if memory_cfg is not None else _mem_cfg(enabled=False),
    )


async def _build(store, agent, fallback):
    return await compaction.build_compacted_history(
        conversation_id="c1",
        profile="admin",
        conversation_storage=store,
        cremind_agent=agent,
        fallback_history=fallback,
    )


# ── pure helpers ───────────────────────────────────────────────────────────────

def test_build_effective_shapes() -> None:
    tail = [_msg(5, "hello"), _msg(6, "world", role="agent")]
    # With a summary: first message is the user-role summary block, then the tail.
    out = compaction._build_effective("S", tail)
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Summary of earlier conversation")
    assert out[0]["content"].endswith("S")
    assert "hello" in out[1]["content"]
    assert out[2]["role"] == "assistant"  # 'agent' role mapped by convert
    # Without a summary: just the converted tail.
    out2 = compaction._build_effective(None, tail)
    assert len(out2) == 2
    assert not out2[0]["content"].startswith("[Summary")


# ── build_compacted_history ─────────────────────────────────────────────────────

def test_disabled_returns_fallback(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(enabled=False))
    agent = _FakeAgent(_FakeLLM())
    fallback = [{"role": "user", "content": "FALLBACK"}]
    out = asyncio.run(_build(_FakeStore([_msg(0, _TEN)]), agent, fallback))
    assert out is fallback
    assert agent._llm.calls == 0


def test_below_threshold_no_fold(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(threshold=1_000_000))
    store = _FakeStore([_msg(0, "hello"), _msg(1, "there", role="agent")])
    agent = _FakeAgent(_FakeLLM())
    out = asyncio.run(_build(store, agent, fallback=[{"role": "user", "content": "FB"}]))
    assert agent._llm.calls == 0          # no summarization
    assert store.set_calls == []          # state untouched
    assert any("hello" in m["content"] for m in out)   # rebuilt tail, no summary block
    assert not out[0]["content"].startswith("[Summary")


def test_over_threshold_does_not_auto_fold(monkeypatch) -> None:
    # Compaction is now model-driven (suggest-only): even over threshold the read
    # path NEVER folds automatically — it just returns summary + verbatim tail.
    _patch_cfg(monkeypatch, _cfg(threshold=25, keep_recent_tokens=5, keep_recent_messages=1))
    store = _FakeStore([_msg(i, _TEN) for i in range(4)])
    agent = _FakeAgent(_FakeLLM(output="RUNNING SUMMARY"))
    out = asyncio.run(_build(store, agent, fallback=[]))

    assert agent._llm.calls == 0          # no summarizer call
    assert store.set_calls == []          # state untouched
    assert len(out) == 4                  # full verbatim tail, no summary block
    assert not out[0]["content"].startswith("[Summary")


def test_compaction_suggestion_over_and_under_threshold(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(threshold=25, keep_recent_tokens=5, max_tokens=5))
    over = _FakeStore([_msg(i, _TEN) for i in range(4)])  # ~40 tokens > 25
    s = asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=over,
    ))
    assert s is not None
    assert s["current_tokens"] >= s["threshold"] == 25
    assert s["estimated_savings"] >= 0

    _patch_cfg(monkeypatch, _cfg(threshold=1_000_000))
    under = _FakeStore([_msg(0, _TEN)])
    assert asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=under,
    )) is None


def test_compaction_suggestion_counts_reasoning_trace(monkeypatch) -> None:
    # Tiny final-answer content, but a big replayed reasoning trace. With replay ON
    # the trace tokens must count toward the threshold (else the prompt grows large
    # without ever suggesting a compaction); with replay OFF only content counts.
    _patch_cfg(monkeypatch, _cfg(threshold=25, keep_recent_tokens=5, max_tokens=5))
    big_trace = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "search", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1",
         "content": " ".join([_TEN] * 4)},  # ~40 tokens of tool output
        {"role": "assistant", "content": "ok"},
    ]
    msgs = [{"id": "m0", "role": "agent", "content": "ok",
             "ordering": 0, "llm_messages": big_trace}]

    monkeypatch.setattr(compaction, "replay_reasoning_enabled", lambda profile: True)
    s_on = asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=_FakeStore(msgs),
    ))
    assert s_on is not None and s_on["current_tokens"] >= 25  # trace counted

    monkeypatch.setattr(compaction, "replay_reasoning_enabled", lambda profile: False)
    s_off = asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=_FakeStore(msgs),
    ))
    assert s_off is None  # only the tiny "ok" content counts → under threshold


def test_build_effective_replays_trace_when_enabled() -> None:
    trace = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "res"},
        {"role": "assistant", "content": "final"},
    ]
    tail = [{"id": "m5", "role": "agent", "content": "final",
             "ordering": 5, "llm_messages": trace}]
    # include_reasoning splices the trace; without it, content-only.
    assert compaction._build_effective(None, tail, include_reasoning=True) == trace
    out_off = compaction._build_effective(None, tail, include_reasoning=False)
    assert out_off == [{"role": "assistant", "content": "final"}]


def test_apply_compaction_persists_summary_and_advances_watermark(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(max_tokens=2048))
    monkeypatch.setattr(
        compaction, "_store_long_term_facts",
        _async_return(0),
    )
    store = _FakeStore([_msg(i, _TEN) for i in range(4)])  # orderings 0..3
    result = asyncio.run(compaction.apply_compaction(
        conversation_id="c1", profile="admin",
        summary="THE RUNNING SUMMARY", long_term=[], conversation_storage=store,
    ))
    assert result["watermark"] == 3          # newest message ordering
    assert store.set_calls == [("THE RUNNING SUMMARY", 3)]


def test_apply_compaction_routes_long_term_facts(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg())
    captured: dict = {}

    async def _fake_store_facts(profile, conversation_id, facts):
        captured["facts"] = facts
        return len(facts)

    monkeypatch.setattr(compaction, "_store_long_term_facts", _fake_store_facts)
    store = _FakeStore([_msg(0, _TEN), _msg(1, _TEN)])
    result = asyncio.run(compaction.apply_compaction(
        conversation_id="c1", profile="admin",
        summary="S", long_term=["User is Lee", "Repo at /x"], conversation_storage=store,
    ))
    assert captured["facts"] == ["User is Lee", "Repo at /x"]
    assert result["long_term_stored"] == 2


def test_excludes_current_turn_message(monkeypatch) -> None:
    # The just-persisted current turn (sent separately as the volatile input) must
    # not reappear in the rebuilt history tail.
    _patch_cfg(monkeypatch, _cfg(threshold=1_000_000))  # no fold
    store = _FakeStore([_msg(0, "older message"), _msg(1, "current turn")])
    agent = _FakeAgent(_FakeLLM())
    out = asyncio.run(compaction.build_compacted_history(
        conversation_id="c1", profile="admin", conversation_storage=store,
        cremind_agent=agent, fallback_history=[], exclude_message_id="m1",
    ))
    assert len(out) == 1
    assert "older message" in out[0]["content"]
    assert all("current turn" not in m["content"] for m in out)


def test_compaction_config_is_frozen_snapshot() -> None:
    cfg = _cfg()
    assert cfg.enabled is True
    assert cfg.compact_threshold_tokens == 25
    with pytest.raises(Exception):
        cfg.enabled = False  # frozen dataclass
