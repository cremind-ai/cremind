"""Channel replies never carry files — not even the ones the agent made.

The stream bus publishes a ``"file"`` event for every FilePart a tool result
carries: the file ``read_file`` opened, the report ``write_file`` wrote, a
screenshot, a converted document. Each one is a conversation ARTIFACT — the web
UI shows it as a chip — and nothing more. The reply forwarder used to upload
every artifact marked ``origin == "created"`` to the platform after the answer,
which turned "the agent worked on a file while reasoning" into "the person on
the platform was sent that file": a message nobody asked for, which cannot be
recalled.

The invariant pinned here: processing conversation file artifacts never invokes
a channel file-send operation — whatever the artifact's provenance, private chat
or platform group, ``normal`` or ``detail`` replies, whatever a legacy
``auto_send_files`` says, replayed or live — while the answer itself goes out
exactly as before. Every case uses a real file on disk, so none of them passes
merely because the path is missing.

A file still reaches the platform when something sends it on purpose; the last
section pins that one explicit send stays one upload even when the same file
also streams past as an artifact. The send tool's own attachment handling is
covered in ``tests/tools/test_send_channel_message.py``.

Drives the real :meth:`BaseChannelAdapter._forward_reply` against a stub bus,
same as ``test_reply_detail_mode``.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import app.channels.base as base_mod
from app.channels import direct_send
from app.channels.attachments import file_fallback_text
from app.channels.base import BaseChannelAdapter
from app.channels.reply_target import group_target

_ANSWER = "Here you go."
_CONV = "conv-1"
_SENDER = "sender-1"
_ROOM = {"id": "g1", "platform_chat_id": "room-1"}
# Bounded, so a forwarder that never reaches a terminal frame fails its test
# instead of hanging the run.
_WAIT_S = 5.0
# What every "this channel can't receive files" notice says. One appearing in a
# reply would mean something tried to send a file the transport could not carry.
_NOTICE_MARK = "can't receive files"


class _Groups:
    """The one group hook a spoken room turn touches."""

    def note_agent_post(self, group_id):
        return None


class _Adapter(BaseChannelAdapter):
    """Records every bubble and every file-send attempt, at both layers.

    The file methods are overridden twice over — the public wrappers and the
    transport hooks they end in — so no route to an upload can slip past the
    two ``*_files`` logs.
    """

    supports_file_send = True

    def __init__(self, *, response_mode="normal", config=None):
        channel = {
            "id": "ch1", "channel_type": "telegram", "mode": "bot", "profile": "p",
            "response_mode": response_mode, "config": {} if config is None else config,
        }
        super().__init__(channel, storage=None)
        self.sent: list[str] = []                 # private-chat bubbles
        self.chat_sent: list[str] = []            # room bubbles
        self.private_files: list[tuple] = []      # (method, sender id, path)
        self.group_files: list[tuple] = []        # (method, chat id, path)
        self.groups = _Groups()

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append(text)

    async def send_to_chat(self, chat_id, text):
        self.chat_sent.append(text)

    async def send_file(self, sender_id, path, **kw):
        self.private_files.append(("send_file", sender_id, path))
        await super().send_file(sender_id, path, **kw)

    async def send_file_strict(self, sender_id, path, **kw):
        self.private_files.append(("send_file_strict", sender_id, path))
        await super().send_file_strict(sender_id, path, **kw)

    async def send_file_to_chat(self, chat_id, path, **kw):
        self.group_files.append(("send_file_to_chat", chat_id, path))
        await super().send_file_to_chat(chat_id, path, **kw)

    async def _send_file(self, sender_id, path, **kw):
        self.private_files.append(("_send_file", sender_id, path))

    async def _send_file_to_chat(self, chat_id, path, **kw):
        self.group_files.append(("_send_file_to_chat", chat_id, path))

    @property
    def uploads(self) -> list[tuple]:
        """What reached a transport: the hooks every file sender ends in."""
        return [c for c in self.private_files + self.group_files if c[0].startswith("_")]


class _NoFileAdapter(_Adapter):
    """A transport with no file support. Its hooks raise like the base's, so a
    file send here would degrade to the fallback notice — visible in the
    bubbles."""

    supports_file_send = False

    async def _send_file(self, sender_id, path, **kw):
        self.private_files.append(("_send_file", sender_id, path))
        await BaseChannelAdapter._send_file(self, sender_id, path, **kw)

    async def _send_file_to_chat(self, chat_id, path, **kw):
        self.group_files.append(("_send_file_to_chat", chat_id, path))
        await BaseChannelAdapter._send_file_to_chat(self, chat_id, path, **kw)


class _Bus:
    """The stream bus as the forwarder uses it.

    ``subscribe`` hands back ``replay`` — the frames of a run already underway —
    and a live queue; :meth:`publish` feeds that queue afterwards, so a live
    frame really reaches the forwarder through the ``queue.get()`` it is blocked
    on. Every unsubscribe is recorded, with whether it released the queue this
    bus handed out.
    """

    def __init__(self, replay=()):
        self.replay = list(replay)
        self.queue: asyncio.Queue | None = None
        self.unsubscribed: list[tuple[str, bool]] = []

    async def subscribe(self, conversation_id):
        self.queue = asyncio.Queue()
        return self.queue, list(self.replay), True

    async def unsubscribe(self, conversation_id, queue):
        self.unsubscribed.append((conversation_id, queue is self.queue))

    def publish(self, *events):
        for event in events:
            self.queue.put_nowait(event)


async def _until(predicate) -> None:
    """Yield to the loop until ``predicate()`` holds (the caller bounds it)."""
    while not predicate():
        await asyncio.sleep(0)


async def _drive(adapter, bus, *, live=(), target=_SENDER) -> None:
    """One forwarder, start to finish: its replay, then ``live`` one frame at a
    time once it has subscribed."""
    async with asyncio.timeout(_WAIT_S):
        task = asyncio.create_task(adapter._forward_reply(_CONV, target))
        await _until(lambda: bus.queue is not None)
        for event in live:
            bus.publish(event)
            await asyncio.sleep(0)
        await task


def _forward(monkeypatch, adapter, *, replay=(), live=(), target=_SENDER) -> _Bus:
    bus = _Bus(replay)
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: bus)
    asyncio.run(_drive(adapter, bus, live=live, target=target))
    return bus


def _touch(tmp_path, name="report.pdf") -> str:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7 report")
    return str(path)


def _file(seq, path, *, origin="created"):
    """A ``file`` frame shaped like the stream runner's
    ``FilePart.model_dump(mode="json")``; ``origin=None`` leaves the
    provenance out entirely."""
    return {
        "seq": seq, "type": "file",
        "data": {
            "kind": "file",
            "file": {
                "name": os.path.basename(path), "mimeType": "application/pdf",
                "uri": path.replace(os.sep, "/"),
            },
            "metadata": None if origin is None else {"origin": origin},
        },
    }


def _thinking(seq, *, action="write_file", action_input='{"path": "report.pdf"}',
              thought="Write the report."):
    return {
        "seq": seq, "type": "thinking",
        "data": {"Step": 1, "Thought": thought, "Action": action,
                 "Action_Input": action_input},
    }


def _result(seq):
    return {"seq": seq, "type": "result",
            "data": {"step": 1, "call_id": f"c{seq}", "Result": "ok"}}


def _text(seq, token=_ANSWER):
    return {"seq": seq, "type": "text", "data": {"token": token}}


def _complete(seq, **flags):
    return {"seq": seq, "type": "complete",
            "data": {"assistant_id": "a1", "errored": False, "cancelled": False, **flags}}


def _turn(path, *, origin="created"):
    """One ordinary turn: the agent writes a file, then answers."""
    return [_thinking(1), _result(2), _file(3, path, origin=origin), _text(4), _complete(5)]


def _assert_no_file_sent(adapter) -> None:
    assert adapter.private_files == []
    assert adapter.group_files == []
    assert not any(_NOTICE_MARK in m for m in adapter.sent + adapter.chat_sent)


# ── the invariant, across every dimension ───────────────────────────────────


_ORIGINS = {
    "created": "created",
    "referenced": "referenced",
    "absent": None,
    "unknown": "exported",
}
_TARGETS = {"dm": _SENDER, "group": group_target(_ROOM)}
_CONFIGS = {
    "key-absent": {},
    "false": {"auto_send_files": False},
    "legacy-true": {"auto_send_files": True},
}


@pytest.mark.parametrize("delivery", ["replay", "live"])
@pytest.mark.parametrize("config", list(_CONFIGS.values()), ids=list(_CONFIGS))
@pytest.mark.parametrize("response_mode", ["normal", "detail"])
@pytest.mark.parametrize("destination", list(_TARGETS))
@pytest.mark.parametrize("origin", list(_ORIGINS.values()), ids=list(_ORIGINS))
def test_a_file_artifact_is_never_sent_with_the_reply(
    monkeypatch, tmp_path, origin, destination, response_mode, config, delivery,
):
    adapter = _Adapter(response_mode=response_mode, config=dict(config))
    path = _touch(tmp_path)
    frames = _turn(path, origin=origin)
    replay, live = (frames, ()) if delivery == "replay" else ((), frames)

    bus = _forward(
        monkeypatch, adapter, replay=replay, live=live, target=_TARGETS[destination],
    )

    _assert_no_file_sent(adapter)
    heard, elsewhere = (
        (adapter.chat_sent, adapter.sent) if destination == "group"
        else (adapter.sent, adapter.chat_sent)
    )
    assert elsewhere == []
    if response_mode == "normal":
        assert heard == [_ANSWER]
    else:
        assert heard[-1] == "*Response*\n\n" + _ANSWER
    assert bus.unsubscribed == [(_CONV, True)]


# ── the stream's edge cases ─────────────────────────────────────────────────


def test_duplicate_artifacts_across_replay_and_live_send_nothing(monkeypatch, tmp_path):
    """A forwarder that subscribes mid-run is handed the ring AND the live
    queue, so a frame can arrive twice; one file can also be reported by
    several tool calls. None of it becomes an upload, and the answer still goes
    out once."""
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    replay = [_thinking(1), _file(2, path), _file(3, path), _text(4)]
    live = [_file(3, path), _text(4), _file(5, path), _complete(6)]   # 3 and 4 overlap

    bus = _forward(monkeypatch, adapter, replay=replay, live=live)

    _assert_no_file_sent(adapter)
    assert adapter.sent == [_ANSWER]
    assert bus.unsubscribed == [(_CONV, True)]


def test_many_files_in_one_turn_send_nothing(monkeypatch, tmp_path):
    adapter = _Adapter(config={"auto_send_files": True})
    paths = [_touch(tmp_path, f"part-{i}.pdf") for i in range(7)]
    frames = (
        [_thinking(1)]
        + [_file(2 + i, p) for i, p in enumerate(paths)]
        + [_text(20), _complete(21)]
    )
    _forward(monkeypatch, adapter, replay=frames)
    _assert_no_file_sent(adapter)
    assert adapter.sent == [_ANSWER]


@pytest.mark.parametrize("response_mode", ["normal", "detail"])
def test_a_file_inside_a_result_observation_is_not_sent_either(
    monkeypatch, tmp_path, response_mode,
):
    """A tool's FilePart rides its ``result`` frame as well as its own ``file``
    frame. Neither is an upload — at most, a ``detail`` step names the file as
    text."""
    adapter = _Adapter(response_mode=response_mode, config={"auto_send_files": True})
    path = _touch(tmp_path)
    observation = [{"kind": "text", "text": "Wrote report.pdf"}, _file(0, path)["data"]]
    frames = [
        _thinking(1),
        {"seq": 2, "type": "result",
         "data": {"step": 1, "call_id": "c1", "Result": "Wrote report.pdf",
                  "Observation": observation}},
        _file(3, path), _text(4), _complete(5),
    ]
    _forward(monkeypatch, adapter, replay=frames)
    _assert_no_file_sent(adapter)
    assert adapter.sent[-1].endswith(_ANSWER)


def test_a_moved_or_missing_file_is_neither_sent_nor_announced(monkeypatch, tmp_path):
    """write→move leaves the first artifact's path dangling, and a file can be
    gone before the turn ends. Nothing goes out for any of them — not even a
    notice, which would announce a file nobody asked for."""
    adapter = _Adapter(config={"auto_send_files": True})
    written = _touch(tmp_path, "draft.pdf")
    moved = str(tmp_path / "final.pdf")
    os.replace(written, moved)
    ghost = str(tmp_path / "never-written.pdf")
    frames = [
        _thinking(1), _file(2, written), _file(3, moved), _file(4, ghost),
        _text(5), _complete(6),
    ]
    _forward(monkeypatch, adapter, replay=frames)
    _assert_no_file_sent(adapter)
    assert adapter.sent == [_ANSWER]


def test_a_file_only_private_turn_sends_no_file_and_invents_no_notice(monkeypatch, tmp_path):
    """The old path counted a file as a whole answer. A turn with no text now
    has nothing to send, and nothing is made up to stand in for the file."""
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    bus = _forward(monkeypatch, adapter, replay=[_thinking(1), _file(2, path), _complete(3)])
    _assert_no_file_sent(adapter)
    assert adapter.sent == []
    assert bus.unsubscribed == [(_CONV, True)]


@pytest.mark.parametrize("answer", [None, "[silent]"])
def test_a_silent_group_turn_posts_nothing_at_all(monkeypatch, tmp_path, answer):
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    frames = [_thinking(1), _file(2, path)]
    if answer is not None:
        frames.append(_text(3, answer))
    frames.append(_complete(4))
    _forward(monkeypatch, adapter, replay=frames, target=group_target(_ROOM))
    _assert_no_file_sent(adapter)
    assert adapter.chat_sent == [] and adapter.sent == []


def test_files_around_a_flow_break_leave_both_replies_intact(monkeypatch, tmp_path):
    """An interrupted turn speaks twice — the interim reply at the flow break,
    then the answer. Artifacts on either side of the break must not swallow,
    hold back or accompany either one."""
    adapter = _Adapter(config={"auto_send_files": True})
    before = _touch(tmp_path, "before.pdf")
    after = _touch(tmp_path, "after.pdf")
    frames = [
        _file(1, before), _text(2, "Not yet, still writing."),
        {"seq": 3, "type": "flow_break", "data": {"message_ids": ["m2"], "step": 1}},
        _file(4, after), _text(5, "Done."), _file(6, after), _complete(7),
    ]
    _forward(monkeypatch, adapter, live=frames)
    _assert_no_file_sent(adapter)
    assert adapter.sent == ["Not yet, still writing.", "Done."]


_FAILED_ENDINGS = {
    "error": {"type": "error",
              "data": {"message": "boom", "code": "agent_run_failed"}},
    "error-cancelled": {"type": "error",
                        "data": {"message": "Stopped by user.", "cancelled": True}},
    "complete-errored": {"type": "complete",
                         "data": {"assistant_id": "a1", "errored": True, "cancelled": False}},
    "complete-cancelled": {"type": "complete",
                           "data": {"assistant_id": "a1", "errored": False, "cancelled": True}},
}


@pytest.mark.parametrize("ending", list(_FAILED_ENDINGS.values()), ids=list(_FAILED_ENDINGS))
@pytest.mark.parametrize("destination", list(_TARGETS))
def test_a_failed_or_stopped_run_sends_no_file(monkeypatch, tmp_path, ending, destination):
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    frames = [_thinking(1), _file(2, path), {"seq": 3, **ending}]
    bus = _forward(monkeypatch, adapter, replay=frames, target=_TARGETS[destination])
    _assert_no_file_sent(adapter)
    assert bus.unsubscribed == [(_CONV, True)]


def test_a_cancelled_forwarder_sends_no_file_and_lets_go_of_the_bus(monkeypatch, tmp_path):
    """Shutdown, a forgotten sender or a blocked group cancel a forwarder while
    it waits. The artifacts it saw stay artifacts, and it still unsubscribes."""
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    bus = _Bus([_thinking(1), _file(2, path), _text(3)])
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: bus)

    async def _go():
        async with asyncio.timeout(_WAIT_S):
            task = asyncio.create_task(adapter._forward_reply(_CONV, _SENDER))
            await _until(lambda: bus.queue is not None)
            bus.publish(_file(4, path))
            await _until(bus.queue.empty)   # taken; blocked on the next frame
            task.cancel()
            await asyncio.wait({task})
            return task

    task = asyncio.run(_go())

    assert task.done()
    _assert_no_file_sent(adapter)
    assert adapter.sent == []               # the answer never completed
    assert bus.unsubscribed == [(_CONV, True)]


@pytest.mark.parametrize("destination", list(_TARGETS))
def test_a_transport_without_file_support_gets_the_answer_alone(
    monkeypatch, tmp_path, destination,
):
    """Where the old path swapped a file for a "can't receive files" notice, the
    reply is now just the answer: there was never a file to announce."""
    assert _NOTICE_MARK in file_fallback_text("report.pdf")  # the marker is real
    adapter = _NoFileAdapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    _forward(monkeypatch, adapter, replay=_turn(path), target=_TARGETS[destination])
    _assert_no_file_sent(adapter)
    heard = adapter.chat_sent if destination == "group" else adapter.sent
    assert heard == [_ANSWER]


def test_the_final_answer_fallback_still_speaks_beside_a_file(monkeypatch, tmp_path):
    """A Final Answer tool call can end a turn with no ``text`` frame at all; its
    Action_Input is then the answer. An artifact in the same turn changes
    nothing about that."""
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    frames = [
        _thinking(1), _file(2, path),
        _thinking(3, action="Final Answer", action_input=_ANSWER, thought="Done."),
        _complete(4),
    ]
    _forward(monkeypatch, adapter, replay=frames)
    _assert_no_file_sent(adapter)
    assert adapter.sent == [_ANSWER]


# ── a file sent on purpose ──────────────────────────────────────────────────


class _Storage:
    """The four ``ConversationStorage`` calls one direct send makes."""

    def __init__(self, row: dict) -> None:
        self._row = row
        self.messages: list[dict] = []

    async def list_senders(self, channel_id):
        return [dict(self._row)]

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None,
                                   phone=None, wa_lid=None):
        return dict(self._row)

    async def ensure_sender_conversation(self, sender, profile, channel_id,
                                         display_name=None):
        return sender["conversation_id"]

    async def add_message(self, conversation_id, role, content=None, metadata=None, **kw):
        msg = {"id": f"m{len(self.messages)}", "conversation_id": conversation_id,
               "role": role, "content": content, "parts": kw.get("parts")}
        self.messages.append(msg)
        return msg


def test_a_file_sent_on_purpose_goes_out_once_and_the_reply_adds_nothing(
    monkeypatch, tmp_path,
):
    """The agent writes a report, then sends it with ``send_channel_message`` —
    what "send me the report" should lead to. The file reaches the platform
    exactly once, from that send. The ``file`` artifact the write left on the
    stream, marked ``created``, does not make it twice, as the old reply path
    would have — legacy ``auto_send_files: true`` included."""
    adapter = _Adapter(config={"auto_send_files": True})
    path = _touch(tmp_path)
    storage = _Storage({
        "id": "row-1", "channel_id": "ch1", "sender_id": _SENDER,
        "display_name": "Lee", "phone": None, "wa_lid": None,
        "authenticated": True, "conversation_id": _CONV,
    })
    bus = _Bus()
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: bus)

    async def _turn_with_a_send():
        async with asyncio.timeout(_WAIT_S):
            task = asyncio.create_task(adapter._forward_reply(_CONV, _SENDER))
            await _until(lambda: bus.queue is not None)
            bus.publish(_thinking(1), _result(2), _file(3, path))
            await _until(bus.queue.empty)
            # The send tool's call, mid-turn, through the shared service.
            summary = await direct_send.send_direct_messages(
                adapters=[adapter], storage=storage,
                recipients=[{"to": _SENDER}], message="The report.",
                dry_run=False, initiated_by="send_channel_message",
                attachments=[{"path": path, "name": "report.pdf",
                              "mime": "application/pdf"}],
            )
            bus.publish(
                _thinking(4, action="send_channel_message", action_input="{}",
                          thought="Send it."),
                _result(5), _text(6, "Sent you the report."), _complete(7),
            )
            await task
            return summary

    summary = asyncio.run(_turn_with_a_send())

    assert summary["sent"] == 1
    assert summary["results"][0]["files_sent"] == 1
    assert adapter.private_files == [
        ("send_file_strict", _SENDER, path), ("_send_file", _SENDER, path),
    ]
    assert adapter.uploads == [("_send_file", _SENDER, path)]
    assert adapter.group_files == []
    assert adapter.sent == ["The report.", "Sent you the report."]
    assert bus.unsubscribed == [(_CONV, True)]
