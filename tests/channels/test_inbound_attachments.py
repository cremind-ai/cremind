"""Inbound channel attachments: descriptors in, staged files out.

Drives the real ``BaseChannelAdapter._handle_inbound`` with descriptor-carrying
messages and asserts the contract the feature stands on:

- the bytes of an attachment are fetched ONLY for a sender who passed the
  channel's auth gate (a stranger's payload never touches disk),
- staged paths ride the same ``attachments=`` kwargs the web composer uses,
  into both the mid-turn park and the enqueue,
- a file-only message still becomes a turn (placeholder text),
- the size cap is enforced both before and after the download,
- collisions rename rather than overwrite,
- and staging is per-profile — two profiles never share a temp tree.

Same no-SDK conventions as ``test_midturn_dispatch``: a ``_Storage`` stub, the
park/enqueue monkeypatched, ``CREMIND_SYSTEM_DIR`` pointed at ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import app.channels.attachments as attachments_mod
import app.channels.base as base_mod
from app.channels.attachments import IncomingFile
from app.channels.base import BaseChannelAdapter
from app.config.settings import BaseConfig


class _Storage:
    """Open-auth by default; ``authenticated`` is injectable per test."""

    def __init__(self, authenticated: bool = True):
        self._authenticated = authenticated

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None,
                                   **_extra):
        return {
            "id": f"s-{sender_id}", "channel_id": channel_id,
            "sender_id": sender_id, "display_name": display_name,
            "authenticated": self._authenticated,
            "conversation_id": f"conv-{sender_id}",
        }

    async def ensure_sender_conversation(self, sender, profile, channel_id,
                                         display_name=None):
        return sender["conversation_id"]

    async def get_messages(self, conversation_id):
        return []


class _Adapter(BaseChannelAdapter):
    def __init__(self, channel, storage):
        super().__init__(channel, storage)
        self.gated: list[tuple] = []

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        return None

    async def _handle_access_gate(self, sender, auth, text):
        self.gated.append((sender["sender_id"], auth, text))

    async def _forward_reply(self, conversation_id, target):
        return None


def _adapter(profile="admin", *, auth="open", authenticated=True) -> _Adapter:
    channel = {
        "id": "c1", "profile": profile, "channel_type": "telegram",
        "mode": "bot", "config": {"subscribe_auth": auth},
    }
    return _Adapter(channel, _Storage(authenticated=authenticated))


@pytest.fixture(autouse=True)
def _tmp_system_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_dispatch(monkeypatch):
    """Capture what reaches the park and the enqueue."""
    state = {"park": [], "enqueue": []}

    async def _park(**kwargs):
        state["park"].append(kwargs)
        return None  # idle → the caller enqueues a fresh turn

    async def _enqueue(**kwargs):
        state["enqueue"].append(kwargs)

    import app.events.user_message_delivery as umd
    monkeypatch.setattr(umd, "try_park_user_message", _park)
    monkeypatch.setattr(base_mod.event_queue, "enqueue_user_message", _enqueue)
    return state


def _descriptor(content: bytes = b"hello", name: str = "note.txt",
                size=None, on_fetch=None, on_discard=None) -> IncomingFile:
    async def fetch(dest_dir: str) -> str:
        if on_fetch is not None:
            on_fetch()
        dest = attachments_mod.dest_for(dest_dir, name)
        with open(dest, "wb") as fh:
            fh.write(content)
        return dest

    async def discard() -> None:
        if on_discard is not None:
            on_discard()

    return IncomingFile(name=name, size=size, fetch=fetch, discard=discard)


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_attachments_reach_both_park_and_enqueue(_stub_dispatch, _tmp_system_dir):
    async def scenario():
        a = _adapter()
        await a._handle_inbound(
            "u1", "Tester", "look at this", files=[_descriptor()],
        )
        await _settle()

        (park,) = _stub_dispatch["park"]
        (enqueue,) = _stub_dispatch["enqueue"]
        for call in (park, enqueue):
            atts = call["attachments"]
            assert atts and atts[0]["name"] == "note.txt"
            assert os.path.isfile(atts[0]["path"])
        # Staged into THIS conversation's temp dir under THIS profile's slice.
        expected_dir = os.path.join(
            str(_tmp_system_dir), "admin", "uploads_tmp", "conv-u1",
        )
        assert os.path.dirname(park["attachments"][0]["path"]) == expected_dir

    asyncio.run(scenario())


def test_a_file_only_message_still_starts_a_turn(_stub_dispatch):
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "", files=[_descriptor()])
        await _settle()

        (enqueue,) = _stub_dispatch["enqueue"]
        assert enqueue["query"] == "[sent a file: note.txt]"
        assert enqueue["attachments"]

    asyncio.run(scenario())


def test_an_unauthenticated_sender_never_triggers_a_fetch(_stub_dispatch):
    """The abuse guard: the gate runs INSTEAD of the download."""
    async def scenario():
        fetched = []
        discarded = []
        a = _adapter(auth="approval", authenticated=False)
        bomb = _descriptor(
            on_fetch=lambda: fetched.append(True),
            on_discard=lambda: discarded.append(True),
        )
        await a._handle_inbound("stranger", None, "hi", files=[bomb])
        await _settle()

        assert fetched == []            # not a byte moved
        assert discarded == [True]      # held resources released
        assert a.gated                  # the gate ran as before
        assert _stub_dispatch["enqueue"] == []

    asyncio.run(scenario())


def test_size_caps_are_enforced_before_and_after_download(
    _stub_dispatch, monkeypatch,
):
    async def scenario():
        monkeypatch.setattr(attachments_mod, "max_upload_bytes", lambda: 10)
        fetched = []
        a = _adapter()
        oversized_declared = _descriptor(
            name="big.bin", size=11, on_fetch=lambda: fetched.append("declared"),
        )
        oversized_actual = _descriptor(
            name="liar.bin", content=b"x" * 20,  # platform reported no size
            on_fetch=lambda: fetched.append("actual"),
        )
        small = _descriptor(name="ok.txt", content=b"tiny")
        await a._handle_inbound(
            "u1", None, "files", files=[oversized_declared, oversized_actual, small],
        )
        await _settle()

        assert "declared" not in fetched          # refused without a download
        assert "actual" in fetched                # downloaded, then removed
        (enqueue,) = _stub_dispatch["enqueue"]
        names = [att["name"] for att in enqueue["attachments"]]
        assert names == ["ok.txt"]                # only the survivor staged
        # The over-cap download did not leave debris behind.
        staged_dir = os.path.dirname(enqueue["attachments"][0]["path"])
        assert sorted(os.listdir(staged_dir)) == ["ok.txt"]

    asyncio.run(scenario())


def test_name_collisions_rename_instead_of_overwriting(_stub_dispatch):
    async def scenario():
        a = _adapter()
        await a._handle_inbound(
            "u1", None, "two files, one name",
            files=[_descriptor(content=b"one"), _descriptor(content=b"two")],
        )
        await _settle()

        (enqueue,) = _stub_dispatch["enqueue"]
        names = sorted(att["name"] for att in enqueue["attachments"])
        assert names == ["note (1).txt", "note.txt"]

    asyncio.run(scenario())


def test_two_profiles_stage_into_their_own_slices(_stub_dispatch, _tmp_system_dir):
    """Check #3: a second profile must not share the first one's temp tree."""
    async def scenario():
        for profile in ("admin", "assistant"):
            a = _adapter(profile=profile)
            await a._handle_inbound("u1", None, "hi", files=[_descriptor()])
            await _settle()

        paths = [
            call["attachments"][0]["path"] for call in _stub_dispatch["enqueue"]
        ]
        assert len(paths) == 2
        assert paths[0].startswith(os.path.join(str(_tmp_system_dir), "admin") + os.sep)
        assert paths[1].startswith(
            os.path.join(str(_tmp_system_dir), "assistant") + os.sep,
        )

    asyncio.run(scenario())
