"""Citations on a messaging platform: "[1]" markers and a "Sources:" footer.

A platform cannot render the web UI's chips, so the reply forwarder rewrites
the answer's ``[ud:…]`` tokens. What is pinned:

- **one numbering per turn**: an interim reply sent at a flow break and the
  final answer are halves of one message, so "[1]" means the same source in
  both — and it is the number the web UI shows (first appearance over the
  whole answer);
- the footer names the file, where in it, and its folder (not in a room,
  where other people read it);
- steps show the source's name instead of a raw token;
- a reply without citations is untouched, and a rendering failure sends the
  reply as it was rather than dropping it.

Drives the real :meth:`BaseChannelAdapter._forward_reply` against a stub bus.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.channels.base as base_mod  # noqa: E402
from app.channels.base import BaseChannelAdapter  # noqa: E402
from app.userdocs import citations as cit  # noqa: E402
from app.userdocs.cite import number_tokens  # noqa: E402
from tests.userdocs._citations_env import build, close  # noqa: E402


class _Bus:
    def __init__(self, events):
        self._events = events

    async def subscribe(self, conversation_id):
        return asyncio.Queue(), list(self._events), False

    async def unsubscribe(self, conversation_id, queue):
        return None


class _Adapter(BaseChannelAdapter):
    def __init__(self, response_mode: str = "normal"):
        channel = {
            "id": "ch1", "channel_type": "telegram", "mode": "bot", "profile": "alice",
            "response_mode": response_mode, "config": {},
        }
        super().__init__(channel, storage=None)
        self.sent: list[str] = []

    async def _run(self):
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append(text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch)
    cit._pending.clear()
    law, c1, c2 = e.law, e.law_chunks[0], e.law_chunks[1]
    note, n1 = e.note, e.note_chunks[0]
    cit.issue("alice", "c-web", [e.issued(law, c1), e.issued(law, c2), e.issued(note, n1)])
    e.t1, e.t2, e.t3 = e.token(law, c1), e.token(law, c2), e.token(note, n1)
    yield e
    close(e)


def _forward(monkeypatch, events, response_mode="normal") -> list[str]:
    adapter = _Adapter(response_mode)
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: _Bus(events))
    asyncio.run(adapter._forward_reply("c-web", "sender-1"))
    return adapter.sent


def _text(seq, s):
    return {"seq": seq, "type": "text", "data": {"token": s}}


_COMPLETE = {"type": "complete", "data": {"errored": False, "cancelled": False}}


def test_interim_and_final_share_one_numbering(env, monkeypatch):
    first = f"Not yet — the law says courts decide {env.t1}."
    second = f"Done. Mediation comes first {env.t2}, as before {env.t1}; revenue grew {env.t3}."
    events = [
        _text(1, first[:30]), _text(2, first[30:]),
        {"seq": 3, "type": "flow_break", "data": {}},
        _text(4, second),
        {"seq": 5, **_COMPLETE},
    ]
    sent = _forward(monkeypatch, events)
    assert len(sent) == 2

    assert sent[0] == (
        "Not yet — the law says courts decide [1].\n\n"
        "Sources:\n[1] luat-dat-dai.pdf · p. 1 · Luat"
    )
    assert sent[1] == (
        "Done. Mediation comes first [2], as before [1]; revenue grew [3].\n\n"
        "Sources:\n"
        "[1] luat-dat-dai.pdf · p. 1 · Luat\n"
        "[2] luat-dat-dai.pdf · p. 2 · Luat\n"
        "[3] q3.md · lines 1 · Notes"
    )
    # The same numbers the web UI derives from the persisted answer.
    assert number_tokens(first + second) == {env.t1: 1, env.t2: 2, env.t3: 3}


def test_steps_name_the_source_instead_of_a_token(env, monkeypatch):
    events = [
        {"seq": 1, "type": "thinking", "data": {
            "Step": 1, "Thought": "Search the law.", "Action": "user_documents__search",
            "Action_Input": "{}",
        }},
        {"seq": 2, "type": "result", "data": {"Observation": [
            {"kind": "text", "text": f"1. Tòa án nhân dân giải quyết {env.t1}"},
        ]}},
        _text(3, f"Courts decide {env.t1}."),
        {"seq": 4, **_COMPLETE},
    ]
    sent = _forward(monkeypatch, events, response_mode="detail")
    step = next(s for s in sent if s.startswith("*Step 1*"))
    assert "[luat-dat-dai.pdf · p. 1]" in step and "ud:" not in step
    assert sent[-1].endswith("[1] luat-dat-dai.pdf · p. 1 · Luat")


def test_a_reply_without_citations_is_untouched(env, monkeypatch):
    sent = _forward(monkeypatch, [_text(1, "Hello! [not a citation]"), {"seq": 2, **_COMPLETE}])
    assert sent == ["Hello! [not a citation]"]


def test_a_rendering_failure_never_drops_the_reply(env, monkeypatch):
    def boom(self, text):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(cit.CitationRenderer, "render_sync", boom)
    answer = f"Courts decide {env.t1}."
    sent = _forward(monkeypatch, [_text(1, answer), {"seq": 2, **_COMPLETE}])
    assert sent == [answer]


def test_unverifiable_sources_say_so(env, monkeypatch):
    answer = f"Invented [ud:zzzzzzzz]. Real {env.t1}. Guessed [ud:{env.law['cite_id']}]."
    sent = _forward(monkeypatch, [_text(1, answer), {"seq": 2, **_COMPLETE}])
    assert sent == [
        "Invented [1]. Real [2]. Guessed [3].\n\n"
        "Sources:\n"
        "[1] unknown source (not verified)\n"
        "[2] luat-dat-dai.pdf · p. 1 · Luat\n"
        "[3] luat-dat-dai.pdf · Luat (unverified)"
    ]


def test_a_room_gets_no_paths_or_links(env):
    text = f"Courts decide {env.t1}."
    plain = cit.render_plain("alice", "c-web", text, include_paths=False)
    assert plain == "Courts decide [1].\n\nSources:\n[1] luat-dat-dai.pdf · p. 1"
    full = cit.render_plain("alice", "c-web", text)
    assert full.endswith("[1] luat-dat-dai.pdf · p. 1 · Luat")
    assert cit.render_plain("alice", "c-web", "No citations.") == "No citations."
