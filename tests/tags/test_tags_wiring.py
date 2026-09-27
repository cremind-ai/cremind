"""The journal hooks as the stream runner actually drives them.

One real turn through :func:`app.agent.stream_runner.run_agent_to_bus` (a fake
agent, the real storage) journals ``assistant.result`` for a chat — titled
the way the chat is about to be named, not "Untitled Chat" — and nothing for a
profile without Tags. An event-run turn journals its run transitions and never
an ``assistant.result``.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a")

import app.agent.stream_runner as sr  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from tests.tags._helpers import enable, rows, run  # noqa: E402


class _Agent:
    def __init__(self, text: str):
        self.text = text

    async def run(self, **kwargs):
        yield {"type": T.CONTENT, "data": self.text}
        yield {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}


@pytest.fixture
def runner(tagenv, monkeypatch):
    from app.storage.conversation_storage import ConversationStorage

    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    import app.storage as storage_pkg
    import app.storage.event_run_storage as ers_mod

    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    ers = ers_mod.EventRunStorage(tagenv.provider)
    monkeypatch.setattr(ers_mod, "_instance", ers)

    class _Bus:
        async def start_run(self, *a, **k):
            return None

        async def end_run(self, *a, **k):
            return None

        def is_active(self, *a, **k):
            return False

        async def publish(self, *a, **k):
            return None

    monkeypatch.setattr(sr, "get_event_stream_bus", lambda: _Bus())
    return cs, ers


async def _turn(cs, conversation_id: str, profile: str, *, event_run_id=None, query="plan my trip to Hue"):
    await sr.run_agent_to_bus(
        cremind_agent=_Agent("Day 1: citadel. Day 2: tombs."),
        conversation_storage=cs,
        conversation_id=conversation_id,
        run_id=sr.make_run_id(conversation_id, kind="msg"),
        profile=profile,
        query=query,
        history_messages=[],
        push_user_message=False,
        event_run_id=event_run_id,
        event_run=bool(event_run_id),
    )


def test_a_chat_turn_journals_its_result(tagenv, runner) -> None:
    cs, _ = runner
    enable(tagenv, "p1")
    conv = run(cs.create_conversation(profile="p1"))
    other = run(cs.create_conversation(profile="p2"))
    run(_turn(cs, conv["id"], "p1"))
    run(_turn(cs, other["id"], "p2"))
    events = rows(tagenv, "SELECT profile, kind, payload FROM tag_events")
    assert [(e["profile"], e["kind"]) for e in events] == [("p1", "assistant.result")]
    payload = json.loads(events[0]["payload"])
    assert payload["title"] == "plan my trip to Hue"
    assert payload["excerpt"].startswith("Day 1: citadel")
    assert payload["errored"] is False and payload["cancelled"] is False


def test_an_event_run_turn_journals_run_transitions_only(tagenv, runner) -> None:
    cs, ers = runner
    enable(tagenv, "p1")
    conv = run(cs.create_conversation(profile="p1", title="Nightly", kind="event_run"))
    created = run(ers.create(profile="p1", source_kind="schedule", subscription_id="s1",
                             conversation_id=conv["id"], label="Nightly digest", action="a"))
    run(_turn(cs, conv["id"], "p1", event_run_id=created["run"]["id"]))
    kinds = [r["kind"] for r in rows(tagenv, "SELECT kind FROM tag_events ORDER BY seq")]
    assert kinds == ["run.started", "run.completed"]
