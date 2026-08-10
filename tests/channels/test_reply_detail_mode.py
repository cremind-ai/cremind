"""What "Reply detail" actually controls on the platform side.

`normal` ("Final answer only") means exactly that: the platform user gets the
answer and nothing else. Everything else the stream carries — the trigger header
that explains what set an event-driven run off, and each Thinking-Process step —
is Cremind's own working. Leaking it to someone who asked for just the answer
reads as a glitch, especially the ``Trigger:/Action:/Content:`` block, which is
scaffolding no end user has context for.

`detail` ("Answer with steps") sends all of it, in stream order.

Drives the real :meth:`BaseChannelAdapter._forward_reply` against a stub bus.
"""

from __future__ import annotations

import asyncio

import pytest

import app.channels.base as base_mod
from app.channels.base import BaseChannelAdapter

_TRIGGER_BLOCK = (
    "Trigger: new-mail\n"
    "Action: report the customer's decision\n"
    "Content:\n"
    "```\nFrom: ABC — approved, deliver on the 21st.\n```"
)
_ANSWER = "ABC approved the quote; delivery is set for the 21st."


class _Bus:
    """Replays a finished run's events, then reports the run as over."""

    def __init__(self, events):
        self._events = events

    async def subscribe(self, conversation_id):
        return asyncio.Queue(), list(self._events), False

    async def unsubscribe(self, conversation_id, queue):
        return None


class _Adapter(BaseChannelAdapter):
    def __init__(self, response_mode: str):
        channel = {
            "id": "ch1", "channel_type": "telegram", "mode": "bot",
            "response_mode": response_mode, "config": {},
        }
        super().__init__(channel, storage=None)
        self.sent: list[str] = []

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append(text)


def _events():
    """One event-driven run: trigger header, one step, the answer, done."""
    return [
        {"seq": 1, "type": "event_trigger_message",
         "data": {"id": "m1", "content": _TRIGGER_BLOCK, "metadata": {}}},
        {"seq": 2, "type": "thinking",
         "data": {"Step": 1, "Thought": "Read the mail.", "Action": "read_email",
                  "Action_Input": "{}"}},
        {"seq": 3, "type": "result", "data": {"Observation": []}},
        {"seq": 4, "type": "text", "data": {"token": _ANSWER}},
        {"seq": 5, "type": "complete", "data": {"errored": False, "cancelled": False}},
    ]


def _forward(monkeypatch, response_mode: str) -> list[str]:
    adapter = _Adapter(response_mode)
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: _Bus(_events()))
    asyncio.run(adapter._forward_reply("conv-1", "sender-1"))
    return adapter.sent


def test_final_answer_only_sends_just_the_answer(monkeypatch):
    sent = _forward(monkeypatch, "normal")
    assert sent == [_ANSWER]


def test_final_answer_only_never_leaks_the_trigger_scaffolding(monkeypatch):
    """The regression this file exists for."""
    body = "\n".join(_forward(monkeypatch, "normal"))
    for fragment in ("Trigger:", "Action:", "Content:", "```"):
        assert fragment not in body, f"{fragment!r} leaked to the platform user"


def test_answer_with_steps_sends_the_trigger_and_the_steps(monkeypatch):
    sent = _forward(monkeypatch, "detail")
    body = "\n".join(sent)
    assert _TRIGGER_BLOCK in sent          # its own bubble, verbatim
    assert "Read the mail." in body        # the step
    assert _ANSWER in body                 # ...and still the answer
    assert len(sent) > 1


def test_a_plain_chat_reply_is_identical_in_both_modes(monkeypatch):
    """No trigger, one step: `normal` must be unaffected by this change."""
    plain = [
        {"seq": 1, "type": "text", "data": {"token": "Hello!"}},
        {"seq": 2, "type": "complete", "data": {"errored": False, "cancelled": False}},
    ]
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: _Bus(plain))
    adapter = _Adapter("normal")
    asyncio.run(adapter._forward_reply("conv-1", "sender-1"))
    assert adapter.sent == ["Hello!"]
