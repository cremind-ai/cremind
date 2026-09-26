"""The stream runner keeps what the automatic document review did.

Two things must survive a reload: the activity trace's label on the agent's
own reads (``origin`` on the persisted step, ``Origin`` on the live frame),
and the turn's ``document_review`` record on the answer's metadata — while
the Sources stay the answer's own citations, not the files the review read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.agent.stream_runner as sr  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")


class _Agent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def run(self, **kwargs):
        for chunk in self.chunks:
            yield chunk


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "runner.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid','p1',0,0)"))
    cs = ConversationStorage(provider)
    cs._initialized = True
    import app.storage as storage_pkg

    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    published: list[tuple] = []

    class _Bus:
        async def start_run(self, *a, **k):
            return None

        async def end_run(self, *a, **k):
            return None

        def is_active(self, *a, **k):
            return False

        async def publish(self, conversation_id, event_type, data=None):
            published.append((event_type, data))

    monkeypatch.setattr(sr, "get_event_stream_bus", lambda: _Bus())
    return cs, published


REVIEW = {"v": 1, "searches": 1, "reads": 0, "returned": 2, "eligible": 2, "examined": ["enenenen", "vivivivi"],
          "partial": [], "failed": [], "unexamined": [], "automatic": {"calls": 2, "tokens": 1331, "rounds": 1},
          "reasons": {}, "cited": []}


def test_the_reads_label_and_the_review_record_survive_a_reload(tmp_path, monkeypatch):
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        chunks = [
            {"type": T.THINKING_ARTIFACT, "data": {"Step": 1, "Call_Id": "c1", "Tool": "documentation_search__search",
                                                    "Tool_Input": "{}"}},
            {"type": T.THINKING_ARTIFACT, "data": {"Step": 1, "Call_Id": "call_docreview_1",
                                                    "Tool": "documentation_search__read", "Tool_Input": "{}",
                                                    "Token_Usage": None, "Origin": "document_review"}},
            {"type": T.CONTENT, "data": "Both guides describe it."},
            {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop",
             "document_review": REVIEW},
        ]
        await sr.run_agent_to_bus(
            cremind_agent=_Agent(chunks), conversation_storage=cs, conversation_id=conv["id"],
            run_id=sr.make_run_id(conv["id"], kind="msg"), profile="p1", query="q", history_messages=[],
            push_user_message=False, update_title_from_query=False,
        )
        row = next(r for r in await cs.get_messages(conv["id"]) if r["role"] == "agent")
        steps = row["thinking_steps"]
        assert "origin" not in steps[0]
        assert steps[1]["origin"] == "document_review"
        assert row["metadata"]["document_review"] == REVIEW
        # The answer cites nothing: no Sources appear for the files the review read.
        assert "citations" not in row["metadata"]
        live = [d for t, d in published if t == "thinking"]
        assert [d.get("Origin") for d in live] == [None, "document_review"]

    asyncio.run(scenario())


def test_a_research_delivery_turn_hands_its_record_to_the_agent(tmp_path, monkeypatch):
    """A research job reporting back: the record its delivery built reaches
    the agent (which answers from it before any model call); any other
    trigger passes none."""

    class _Capturing(_Agent):
        def __init__(self):
            super().__init__([{"type": T.DONE, "data": "summary", "input_tokens": 0, "output_tokens": 0}])
            self.kwargs: list[dict] = []

        async def run(self, **kwargs):
            self.kwargs.append(kwargs)
            async for chunk in super().run(**kwargs):
                yield chunk

    async def scenario():
        cs, _published = _setup(tmp_path, monkeypatch)
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        record = {"job_id": "j1", "status": "partial", "insufficiency": "summary"}
        for trigger in ({"kind": "research_result", "event_type": "document research partial: q", "action": "",
                         "content": "Research job j1", "job_id": "j1", "research_delivery": record},
                        {"kind": "event_task_result", "event_type": "done", "action": "", "content": "x",
                         "research_delivery": record}):
            agent = _Capturing()
            await sr.run_agent_to_bus(
                cremind_agent=agent, conversation_storage=cs, conversation_id=conv["id"],
                run_id=sr.make_run_id(conv["id"], kind="research"), profile="p1", query="q", history_messages=[],
                push_user_message=False, update_title_from_query=False, trigger_event=trigger,
            )
            yield_kw = agent.kwargs[0]
            if trigger["kind"] == "research_result":
                assert yield_kw["research_delivery"] == record
            else:
                assert "research_delivery" not in yield_kw

    asyncio.run(scenario())


def test_a_turn_without_documents_stores_no_review(tmp_path, monkeypatch):
    async def scenario():
        cs, _published = _setup(tmp_path, monkeypatch)
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        await sr.run_agent_to_bus(
            cremind_agent=_Agent([{"type": T.CONTENT, "data": "hi"},
                                  {"type": T.DONE, "input_tokens": 1, "output_tokens": 1}]),
            conversation_storage=cs, conversation_id=conv["id"], run_id=sr.make_run_id(conv["id"], kind="msg"),
            profile="p1", query="q", history_messages=[], push_user_message=False, update_title_from_query=False,
        )
        row = next(r for r in await cs.get_messages(conv["id"]) if r["role"] == "agent")
        assert "document_review" not in (row["metadata"] or {})

    asyncio.run(scenario())
