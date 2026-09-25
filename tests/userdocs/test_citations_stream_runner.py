"""The stream runner checks an answer's citations before saving it.

The verdict has to be part of the persisted message (a reload must show the
same chips the live stream did), and it has to reach live clients before the
turn's ``complete`` frame (the CLI stops listening there). Neither may cost a
turn that cites nothing, nor lose a message when the check fails.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402

import app.agent.stream_runner as sr  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.userdocs import citations as cit  # noqa: E402

from ._citations_env import build, close  # noqa: E402

_DONE = {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}


class _Agent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def run(self, **kwargs):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch)
    Base.metadata.tables["event_runs"].create(bind=e.provider.sync_engine(), checkfirst=True)
    cs = ConversationStorage(e.provider)
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
    e.cs, e.published = cs, published
    yield e
    close(e)


def _run(env, text):
    chunks = [{"type": T.CONTENT, "data": text}, _DONE]
    asyncio.run(sr.run_agent_to_bus(
        cremind_agent=_Agent(chunks),
        conversation_storage=env.cs,
        conversation_id="c-web",
        run_id=sr.make_run_id("c-web", kind="msg"),
        profile="alice",
        query="what does the law say?",
        history_messages=[],
        push_user_message=False,
        update_title_from_query=False,
    ))
    rows = asyncio.run(env.cs.get_messages("c-web"))
    return next(r for r in rows if r["role"] == "agent")


def test_the_verdict_is_saved_and_published_before_complete(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    answer = f"Courts decide {env.token(law, c1)} and invented [ud:zzzzzzzz]."
    row = _run(env, answer)

    assert row["content"] == answer  # the text is never edited
    meta = row["metadata"]["citations"]
    assert [i["status"] for i in meta["items"]] == ["verified", "invalid"]
    assert meta["unverified"] == 1

    types = [t for t, _ in env.published]
    assert "citations" in types
    assert types.index("citations") < types.index("complete")
    payload = dict(env.published)["citations"]
    assert payload == {"citations": meta, "assistant_id": row["id"]}


def test_an_answer_without_citations_is_not_checked(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("finalize_citations ran for an answer that cites nothing")

    monkeypatch.setattr(cit, "finalize_citations", boom)
    row = _run(env, "Nothing from your documents was needed.")
    assert "citations" not in (row["metadata"] or {})
    assert "citations" not in [t for t, _ in env.published]


def test_a_failed_check_never_loses_the_message(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(cit, "finalize_citations", boom)
    row = _run(env, "Courts decide [ud:zzzzzzzz].")
    assert row["content"] == "Courts decide [ud:zzzzzzzz]."
    assert "citations" not in (row["metadata"] or {})
