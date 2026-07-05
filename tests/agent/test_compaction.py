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


def _agent_msg(context_tokens, *, provider="p", model="m", ordering=99) -> dict:
    """Latest agent message carrying the model-reported context size + stamped model."""
    return {
        "id": "a", "role": "agent", "content": "ok", "ordering": ordering,
        "token_usage": {"context_tokens": context_tokens},
        "metadata": {"provider": provider, "model": model},
    }


class _FakeStore:
    def __init__(self, messages, summary=None, watermark=-1, latest_agent=None):
        self._messages = messages
        self._summary = summary
        self._watermark = watermark
        self._latest_agent = latest_agent
        self.set_calls: list[tuple] = []

    async def get_compaction_state(self, cid):
        return self._summary, self._watermark, None

    async def get_messages_after(self, cid, after, limit=5000, newest_first=False):
        rows = [m for m in self._messages if m["ordering"] > after]
        if newest_first:
            # newest `limit` in chronological order (frontier-anchored)
            return sorted(rows, key=lambda m: m["ordering"])[-limit:]
        return rows[:limit]

    async def get_max_ordering(self, cid):
        return max((m["ordering"] for m in self._messages), default=-1)

    async def get_latest_agent_message(self, cid):
        return self._latest_agent

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


def _cfg(*, enabled=True, auto_compact_enabled=False, percent=85.0, keep_recent_tokens=5,
         keep_recent_messages=1, fold_target_percent=60.0, max_tokens=2048) -> CompactionConfig:
    return CompactionConfig(
        enabled=enabled,
        auto_compact_enabled=auto_compact_enabled,
        compact_threshold_percent=percent,
        keep_recent_tokens=keep_recent_tokens,
        keep_recent_messages=keep_recent_messages,
        fold_target_percent=fold_target_percent,
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
    _patch_cfg(monkeypatch, _cfg())
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
    _patch_cfg(monkeypatch, _cfg(keep_recent_tokens=5, keep_recent_messages=1))
    store = _FakeStore([_msg(i, _TEN) for i in range(4)])
    agent = _FakeAgent(_FakeLLM(output="RUNNING SUMMARY"))
    out = asyncio.run(_build(store, agent, fallback=[]))

    assert agent._llm.calls == 0          # no summarizer call
    assert store.set_calls == []          # state untouched
    assert len(out) == 4                  # full verbatim tail, no summary block
    assert not out[0]["content"].startswith("[Summary")


def test_context_tokens_from_records_picks_final_step() -> None:
    # The final reasoning step (max step_index) is the largest prompt; tool/subagent
    # rows are ignored; full prompt = input + cache_read + cache_creation.
    recs = [
        {"source_kind": "reasoning", "step_index": 1,
         "input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"source_kind": "reasoning", "step_index": 3,
         "input_tokens": 5, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 2},
        {"source_kind": "reasoning", "step_index": 2,
         "input_tokens": 99, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"source_kind": "tool", "step_index": 9,
         "input_tokens": 9999, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ]
    assert compaction.context_tokens_from_records(recs) == 207  # step 3: 5 + 200 + 2
    assert compaction.context_tokens_from_records([]) is None
    assert compaction.context_tokens_from_records(
        [{"source_kind": "tool", "step_index": 1, "input_tokens": 5}]) is None


def test_context_usage_threshold_from_window(monkeypatch) -> None:
    # current = the latest agent message's reported context size; threshold =
    # percent/100 * the active model's context window.
    _patch_cfg(monkeypatch, _cfg(percent=85))
    monkeypatch.setattr(compaction, "context_window_for", lambda p, m: 1000)
    store = _FakeStore([], latest_agent=_agent_msg(900))
    usage = asyncio.run(compaction.context_usage(
        conversation_id="c1", profile="admin", conversation_storage=store,
    ))
    # current = max(trace-aware estimate of the (empty) history, reported 900) = 900.
    assert usage["current_tokens"] == 900
    assert usage["context_window"] == 1000
    assert usage["threshold"] == 850
    # New keys expose the reply reserve and the hard floor ceiling.
    assert "response_reserve" in usage and "ceiling" in usage


def test_context_usage_defaults_when_no_turn(monkeypatch) -> None:
    # No agent turn yet → current 0; unknown window → DEFAULT_CONTEXT_WINDOW.
    from app.lib.llm.pricing import DEFAULT_CONTEXT_WINDOW
    _patch_cfg(monkeypatch, _cfg(percent=50))
    monkeypatch.setattr(compaction, "context_window_for", lambda p, m: None)
    usage = asyncio.run(compaction.context_usage(
        conversation_id="c1", profile="admin", conversation_storage=_FakeStore([], latest_agent=None),
    ))
    assert usage["current_tokens"] == 0
    assert usage["context_window"] == DEFAULT_CONTEXT_WINDOW
    assert usage["threshold"] == round(50 / 100 * DEFAULT_CONTEXT_WINDOW)


def test_compaction_suggestion_over_and_under_threshold(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(percent=85, keep_recent_tokens=5, max_tokens=5))
    monkeypatch.setattr(compaction, "context_window_for", lambda p, m: 1000)  # threshold 850

    over = _FakeStore([], latest_agent=_agent_msg(900))
    s = asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=over,
    ))
    assert s is not None
    assert s["current_tokens"] == 900
    assert s["threshold"] == 850
    assert s["context_window"] == 1000
    assert s["estimated_savings"] >= 0

    under = _FakeStore([], latest_agent=_agent_msg(100))
    assert asyncio.run(compaction.compaction_suggestion(
        conversation_id="c1", profile="admin", conversation_storage=under,
    )) is None


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


def test_apply_compaction_persists_summary_and_leaves_keep_recent_tail(monkeypatch) -> None:
    # L4: the fold no longer collapses everything — it leaves a boundary-safe verbatim
    # tail. With keep_recent_messages=1 (and ~15-token messages > keep_recent_tokens=5),
    # the newest message (ordering 3) stays verbatim, so the watermark lands at 2.
    _patch_cfg(monkeypatch, _cfg(keep_recent_tokens=5, keep_recent_messages=1, max_tokens=2048))
    monkeypatch.setattr(
        compaction, "_store_long_term_facts",
        _async_return(0),
    )
    store = _FakeStore([_msg(i, _TEN) for i in range(4)])  # orderings 0..3
    result = asyncio.run(compaction.apply_compaction(
        conversation_id="c1", profile="admin",
        summary="THE RUNNING SUMMARY", long_term=[], conversation_storage=store,
    ))
    assert result["watermark"] == 2          # newest message (3) kept verbatim
    assert store.set_calls == [("THE RUNNING SUMMARY", 2)]


def test_apply_compaction_empty_summary_is_noop(monkeypatch) -> None:
    # An empty/refused summary must NOT advance the watermark (that would silently
    # drop un-summarized messages).
    _patch_cfg(monkeypatch, _cfg())
    monkeypatch.setattr(compaction, "_store_long_term_facts", _async_return(0))
    store = _FakeStore([_msg(i, _TEN) for i in range(4)], summary="OLD", watermark=1)
    result = asyncio.run(compaction.apply_compaction(
        conversation_id="c1", profile="admin",
        summary="   ", long_term=[], conversation_storage=store,
    ))
    assert result.get("skipped") == "empty_summary"
    assert result["watermark"] == 1          # unchanged
    assert store.set_calls == []             # state never written


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
    _patch_cfg(monkeypatch, _cfg())  # no fold
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
    assert cfg.compact_threshold_percent == 85.0
    with pytest.raises(Exception):
        cfg.enabled = False  # frozen dataclass


# ── trace-aware estimator + deterministic floor (L1/L3) ─────────────────────────

def _fold_turn(n: int, big: int = 800) -> list[dict]:
    """A wire-shaped turn: user → assistant(tool_call) → tool result → assistant."""
    return [
        {"role": "user", "content": f"question {n} " + "x" * 40},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": str(n), "type": "function",
             "function": {"name": "exec", "arguments": "a" * big}},
        ]},
        {"role": "tool", "tool_call_id": str(n), "content": "r" * big},
        {"role": "assistant", "content": f"answer {n}"},
    ]


def test_estimate_prompt_tokens_is_trace_aware() -> None:
    content_only = [{"role": "user", "content": "x" * 400}]
    trace = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "exec", "arguments": "a" * 4000}}]},
        {"role": "tool", "tool_call_id": "1", "content": "b" * 8000},
    ]
    # The trace (tool args + tool result) dwarfs a same-glance content-only message.
    assert compaction.estimate_prompt_tokens(trace) > compaction.estimate_prompt_tokens(content_only) * 10


def test_enforce_ceiling_fits_user_first_and_deterministic() -> None:
    import json
    sh = compaction._SUMMARY_HEADER
    hist = [{"role": "user", "content": sh + "running summary " + "s" * 120}]
    for n in range(1, 6):
        hist += _fold_turn(n)
    ceiling = compaction.estimate_prompt_tokens(hist) // 3
    out = compaction.enforce_ceiling(hist, ceiling)
    assert compaction.estimate_prompt_tokens(out) <= ceiling      # fits
    assert out[0]["role"] == "user"                                # Anthropic first-msg rule
    # deterministic in (history, ceiling) → byte-identical prefix across calls
    assert json.dumps(compaction.enforce_ceiling(hist, ceiling)) == json.dumps(out)


def test_enforce_ceiling_never_starts_with_orphan_tool_result() -> None:
    sh = compaction._SUMMARY_HEADER
    hist = [{"role": "user", "content": sh + "s"}]
    for n in range(1, 5):
        hist += _fold_turn(n, big=2000)
    out = compaction.enforce_ceiling(hist, compaction.estimate_prompt_tokens(hist) // 4)
    assert out[0]["role"] == "user"
    rest = out[1:] if out[0]["content"].startswith(sh) else out
    if rest:
        assert rest[0].get("role") != "tool"     # never an orphaned tool result


def test_enforce_ceiling_monster_turn_omits_to_fit() -> None:
    huge = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "z" * 200000}]
    out = compaction.enforce_ceiling(list(huge), 500)
    assert compaction.estimate_prompt_tokens(out) <= 500
    assert out[0]["role"] == "user"


def test_build_compacted_history_floor_runs_even_when_disabled(monkeypatch) -> None:
    # The deterministic floor is UNCONDITIONAL: a giant raw history is clamped even
    # with compaction disabled, so the prompt can never overflow.
    _patch_cfg(monkeypatch, _cfg(enabled=False))

    async def _limits(cid, storage):
        return 200, 50      # window 200, reserve 50 → ceiling 150

    monkeypatch.setattr(compaction, "_model_limits", _limits)
    fallback = [{"role": "user", "content": "u"}] + [
        {"role": "assistant", "content": "z" * 5000},
    ]
    out = asyncio.run(compaction.build_compacted_history(
        conversation_id="c1", profile="admin",
        conversation_storage=_FakeStore([]), fallback_history=fallback,
    ))
    assert compaction.estimate_prompt_tokens(out) <= 150
    assert out[0]["role"] == "user"


# ── keep-recent boundary (L4) + frontier reads ─────────────────────────────────

def test_find_boundary_watermark_snaps_to_turn_start() -> None:
    rows = []
    for i in range(10):
        if i % 2 == 0:
            rows.append({"ordering": i, "role": "user", "content": "u" * 400, "llm_messages": None})
        else:
            rows.append({"ordering": i, "role": "agent", "content": "a" * 400,
                         "llm_messages": [{"role": "assistant", "content": "a" * 400}]})
    wm = compaction.find_boundary_watermark(
        rows, keep_recent_tokens=200, keep_recent_messages=2,
        include_reasoning=True, default=-1,
    )
    assert -1 <= wm < 9                                  # below the frontier
    tail = [r for r in rows if r["ordering"] > wm]
    assert compaction._row_is_turn_start(tail[0], include_reasoning=True)


def test_get_messages_after_is_frontier_anchored(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)
    _seed(store, n_messages=10)                          # orderings 0..9

    async def run():
        newest3 = await store.get_messages_after("c1", -1, limit=3, newest_first=True)
        assert [m["ordering"] for m in newest3] == [7, 8, 9]     # frontier, chronological
        oldest3 = await store.get_messages_after("c1", -1, limit=3)
        assert [m["ordering"] for m in oldest3] == [0, 1, 2]     # legacy oldest-anchored
        assert await store.get_max_ordering("c1") == 9

    asyncio.run(run())


# ── auto-fold decision (L2/L5) + overflow classifier (L3b) ─────────────────────

def test_auto_fold_threshold_sits_between_suggest_and_ceiling() -> None:
    t = compaction._auto_fold_threshold(window=100000, suggest_percent=85, ceiling=95000)
    assert 85000 < t < 95000
    # clamped strictly below the ceiling even when the offset would exceed it
    t2 = compaction._auto_fold_threshold(window=100000, suggest_percent=90, ceiling=91000)
    assert 90000 < t2 < 91000


def test_after_turn_compaction_suggests_when_auto_off(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(auto_compact_enabled=False, percent=85))
    monkeypatch.setattr(compaction, "context_window_for", lambda p, m: 1000)
    store = _FakeStore([], latest_agent=_agent_msg(900))
    evt = asyncio.run(compaction.after_turn_compaction(None, "c1", "admin", store, context_id="c1"))
    assert evt is not None and evt["type"] == "compaction_suggested"


def test_after_turn_compaction_auto_folds_when_enabled(monkeypatch) -> None:
    _patch_cfg(monkeypatch, _cfg(auto_compact_enabled=True, percent=85))
    monkeypatch.setattr(compaction, "context_window_for", lambda p, m: 1000)

    async def _fake_fold(*args, **kwargs):
        return True

    monkeypatch.setattr(compaction, "run_model_fold", _fake_fold)
    store = _FakeStore([], latest_agent=_agent_msg(980))
    evt = asyncio.run(compaction.after_turn_compaction(object(), "c1", "admin", store, context_id="c1"))
    assert evt is not None and evt["type"] == "compaction_auto_folded"


def test_is_context_overflow_classifier() -> None:
    from app.lib.llm.base import is_context_overflow
    assert is_context_overflow("Error: prompt is too long: 250000 tokens > 200000 maximum")
    assert is_context_overflow(Exception("context_length_exceeded"))
    assert not is_context_overflow("connection reset by peer")
    assert not is_context_overflow("")
