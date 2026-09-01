"""Auto-delivery of a reply's files to the channel user.

The stream bus publishes a ``"file"`` event for every FilePart a tool result
carries — including files the agent merely READ (``read_file`` returns one for
the file it opened). The forwarder must therefore deliver only files whose
provenance is ``origin == "created"``, once, after the final answer — and a
silent group turn must ship nothing at all.

Drives the real :meth:`BaseChannelAdapter._forward_reply` against a stub bus,
same as ``test_reply_detail_mode``.
"""

from __future__ import annotations

import asyncio

import pytest

import app.channels.base as base_mod
from app.channels.base import BaseChannelAdapter
from app.channels.reply_target import group_target

_ANSWER = "Here you go."


class _Bus:
    def __init__(self, events):
        self._events = events

    async def subscribe(self, conversation_id):
        return asyncio.Queue(), list(self._events), False

    async def unsubscribe(self, conversation_id, queue):
        return None


class _Groups:
    """The two group hooks the forwarder touches on a spoken turn."""

    def note_agent_post(self, group_id):
        return None


class _Adapter(BaseChannelAdapter):
    supports_file_send = True

    def __init__(self, config=None):
        channel = {
            "id": "ch1", "channel_type": "telegram", "mode": "bot",
            "response_mode": "normal", "config": config or {},
        }
        super().__init__(channel, storage=None)
        self.sent: list[str] = []
        self.files: list[tuple[str, str]] = []       # (address, path)
        self.chat_files: list[tuple[str, str]] = []
        self.groups = _Groups()

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append(text)

    async def send_to_chat(self, chat_id, text):
        self.sent.append(text)

    async def _send_file(self, sender_id, path, *, name=None, mime=None,
                         caption=None):
        self.files.append((sender_id, path))

    async def _send_file_to_chat(self, chat_id, path, *, name=None, mime=None,
                                 caption=None):
        self.chat_files.append((chat_id, path))


class _NoFileAdapter(_Adapter):
    """A transport with no file support — the base defaults raise."""

    supports_file_send = False

    async def _send_file(self, sender_id, path, *, name=None, mime=None,
                         caption=None):
        return await BaseChannelAdapter._send_file(
            self, sender_id, path, name=name, mime=mime, caption=caption,
        )


def _file_event(seq, uri, *, origin="created", name="report.pdf"):
    return {
        "seq": seq, "type": "file",
        "data": {
            "kind": "file",
            "file": {"name": name, "mimeType": "application/pdf", "uri": uri},
            "metadata": {"origin": origin},
        },
    }


def _events(*middle, answer=_ANSWER):
    tail_seq = 90
    out = list(middle)
    if answer is not None:
        out.append({"seq": tail_seq, "type": "text", "data": {"token": answer}})
    out.append({
        "seq": tail_seq + 1, "type": "complete",
        "data": {"errored": False, "cancelled": False},
    })
    return out


def _forward(monkeypatch, adapter, events, target="sender-1"):
    monkeypatch.setattr(base_mod, "get_event_stream_bus", lambda: _Bus(events))
    asyncio.run(adapter._forward_reply("conv-1", target))


def _touch(tmp_path, name="report.pdf") -> str:
    path = tmp_path / name
    path.write_bytes(b"pdf")
    return str(path)


def test_a_created_file_is_delivered_once_after_the_answer(monkeypatch, tmp_path):
    adapter = _Adapter()
    uri = _touch(tmp_path)
    _forward(monkeypatch, adapter, _events(_file_event(1, uri)))
    assert adapter.sent == [_ANSWER]          # the answer went out first
    assert adapter.files == [("sender-1", uri)]


def test_a_referenced_file_is_never_delivered(monkeypatch, tmp_path):
    """read_file publishes file events too — reading must not become sending."""
    adapter = _Adapter()
    uri = _touch(tmp_path)
    _forward(
        monkeypatch, adapter,
        _events(_file_event(1, uri, origin="referenced")),
    )
    assert adapter.sent == [_ANSWER]
    assert adapter.files == []


def test_duplicate_uris_are_delivered_once(monkeypatch, tmp_path):
    adapter = _Adapter()
    uri = _touch(tmp_path)
    _forward(
        monkeypatch, adapter,
        _events(_file_event(1, uri), _file_event(2, uri)),
    )
    assert adapter.files == [("sender-1", uri)]


def test_a_vanished_file_is_skipped(monkeypatch, tmp_path):
    """A write→move sequence leaves the written uri dangling by completion."""
    adapter = _Adapter()
    gone = str(tmp_path / "moved-away.pdf")   # never created
    kept = _touch(tmp_path, "kept.pdf")
    _forward(
        monkeypatch, adapter,
        _events(_file_event(1, gone), _file_event(2, kept, name="kept.pdf")),
    )
    assert adapter.files == [("sender-1", kept)]


def test_the_per_reply_cap_holds(monkeypatch, tmp_path):
    adapter = _Adapter()
    uris = [_touch(tmp_path, f"f{i}.pdf") for i in range(base_mod._MAX_REPLY_FILES + 2)]
    events = _events(*[
        _file_event(i + 1, uri, name=f"f{i}.pdf") for i, uri in enumerate(uris)
    ])
    _forward(monkeypatch, adapter, events)
    assert len(adapter.files) == base_mod._MAX_REPLY_FILES


def test_a_silent_group_turn_ships_no_files(monkeypatch, tmp_path):
    """The agent decided the message was not for it; a file landing anyway
    would be the loudest possible way to be wrong about that."""
    adapter = _Adapter()
    uri = _touch(tmp_path)
    target = group_target({"id": "g1", "platform_chat_id": "room-1"})
    _forward(
        monkeypatch, adapter,
        _events(_file_event(1, uri), answer=None),   # no text → silent in a room
        target=target,
    )
    assert adapter.sent == []
    assert adapter.chat_files == []


def test_a_spoken_group_turn_delivers_into_the_room(monkeypatch, tmp_path):
    adapter = _Adapter()
    uri = _touch(tmp_path)
    target = group_target({"id": "g1", "platform_chat_id": "room-1"})
    _forward(monkeypatch, adapter, _events(_file_event(1, uri)), target=target)
    assert adapter.sent == [_ANSWER]
    assert adapter.chat_files == [("room-1", uri)]


def test_a_file_only_dm_reply_still_delivers(monkeypatch, tmp_path):
    """A turn whose entire output is a file is a legitimate answer."""
    adapter = _Adapter()
    uri = _touch(tmp_path)
    _forward(monkeypatch, adapter, _events(_file_event(1, uri), answer=None))
    assert adapter.files == [("sender-1", uri)]


def test_an_unsupported_transport_degrades_to_a_notice(monkeypatch, tmp_path):
    adapter = _NoFileAdapter()
    uri = _touch(tmp_path)
    _forward(monkeypatch, adapter, _events(_file_event(1, uri)))
    assert adapter.files == []
    notice = adapter.sent[-1]
    assert "report.pdf" in notice
    assert uri not in notice                  # never the server path


def test_an_errored_run_ships_nothing(monkeypatch, tmp_path):
    adapter = _Adapter()
    uri = _touch(tmp_path)
    events = [
        _file_event(1, uri),
        {"seq": 2, "type": "error", "data": {"message": "boom"}},
    ]
    _forward(monkeypatch, adapter, events)
    assert adapter.files == []


def test_auto_send_files_false_disables_delivery(monkeypatch, tmp_path):
    adapter = _Adapter(config={"auto_send_files": False})
    uri = _touch(tmp_path)
    _forward(monkeypatch, adapter, _events(_file_event(1, uri)))
    assert adapter.sent == [_ANSWER]          # the answer still flows
    assert adapter.files == []
