"""An image the room shared while the agent stayed quiet is still findable.

Most group messages never start a turn: the relevance judge declines, and the
message is quiet-written as context. When one of those carries a file, the file
is staged on disk and recorded on the row — but the turn that would have told
the agent its path never ran. So when somebody finally asks "@Bobo what is that
picture?", the agent's history has to carry the path itself, or the agent knows
only a filename and starts guessing at directories.

This is the end of that chain: quiet-write with attachments, then read the
history the next turn is handed, and require the absolute path to be in it.
"""

from __future__ import annotations

import asyncio

from app.channels.groups import dispatch

from .conftest import make_adapter


def _staged(tmp_path, name: str = "photo.jpg") -> list[dict]:
    """What ``stage_incoming_files`` returns: the file, already on disk."""
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff")
    return [{"name": name, "path": str(path)}]


def test_a_quiet_message_leaves_its_files_path_in_the_next_turns_history(tmp_path):
    adapter = make_adapter()
    staged = _staged(tmp_path)

    async def _scenario():
        await dispatch._quiet_write(
            adapter,
            "conv-1",
            "Lý Nguyen: [sent a file: photo.jpg]",
            {"channel_group": {"group_id": "g-1", "quiet": True}},
            attachments=staged,
        )
        return await dispatch._history(adapter, "conv-1")

    history = asyncio.run(_scenario())

    (msg,) = history
    assert msg["role"] == "user"
    # The row's own text — what the room saw.
    assert "[sent a file: photo.jpg]" in msg["content"]
    # And the path, which is the only form the agent can act on.
    assert str(tmp_path / "photo.jpg") in msg["content"]


def test_a_quiet_message_without_files_is_unchanged():
    """The common case by far. It must render exactly as it always did, or
    every group history shifts and the prompt cache misses."""
    adapter = make_adapter()

    async def _scenario():
        await dispatch._quiet_write(
            adapter,
            "conv-1",
            "Alexa: morning all",
            {"channel_group": {"group_id": "g-1", "quiet": True}},
        )
        return await dispatch._history(adapter, "conv-1")

    assert asyncio.run(_scenario()) == [
        {"role": "user", "content": "Alexa: morning all"},
    ]
