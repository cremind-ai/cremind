"""The skill-load cwd anchor is the third cwd writer, and the one left behind.

``change_working_directory`` and ``app.api.files`` were both taught that a
group-chat seat addresses its two storage layers by different ids — the
in-memory override by ``context_id`` (``group:<gid>:<profile>``), the durable
column and the SSE channel by the conversation ROW id. Loading a skill anchors
the cwd too, from inside ``ReasoningAgent._handle_skill_call``, and it kept
using ``context_id`` for all three: in a seat the anchor matched no row (lost on
restart) and published on a channel nobody subscribes to (the room's file tree
never moved), while looking perfectly correct in an ordinary conversation where
the two ids are equal.

Asserted on the source because the surrounding generator needs a live tool
registry, a real SKILL.md and a full agent to reach the anchor — the same reason
``test_task_read_chain_depth`` reads this method rather than driving it.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("a2a")

from app.agent.reasoning_agent import ReasoningAgent  # noqa: E402


def _anchor_block() -> str:
    """The cwd-anchor section of the skill load path."""
    src = inspect.getsource(ReasoningAgent._handle_skill_call)
    start = src.index("Anchor the conversation working directory")
    return src[start:]


def _call_args(block: str, opening: str) -> str:
    """The argument list of one call, paren-balanced.

    ``str(dir_path)`` and ``get_event_stream_bus()`` both close a paren before
    the arguments end, so scanning to the first ``)`` reads the wrong slice.
    """
    start = block.index(opening) + len(opening)
    depth = 1
    for offset, char in enumerate(block[start:]):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return block[start:start + offset]
    raise AssertionError(f"unbalanced call for {opening!r}")


def test_the_anchor_resolves_the_row_id_before_writing():
    block = _anchor_block()
    assert "resolve_cwd_scope(" in block
    assert "row_id" in block


def test_the_durable_write_and_the_publish_both_use_the_row_id():
    """Not ``context_id`` — under a seat's it writes nothing and tells no one."""
    block = _anchor_block()

    persist_args = _call_args(block, "await persist_working_directory(")
    assert "row_id" in persist_args
    assert "self.context_id" not in persist_args

    publish_args = _call_args(block, "await get_event_stream_bus().publish(")
    assert "row_id" in publish_args
    assert "self.context_id" not in publish_args


def test_the_in_memory_override_stays_keyed_by_context_id():
    """The one write that must NOT be re-pointed.

    ``context_id`` is the only key the agent and the built-in tools read the
    override back under; resolving this one to the row id would fix the restart
    case and break the running turn.
    """
    block = _anchor_block()
    assert "set_in_memory_override(self.context_id" in block


def test_a_failed_resolve_still_anchors_the_ordinary_conversation():
    """``row_id`` is seeded from ``context_id`` before the try block.

    Storage that is missing or erroring must leave the pre-seat behaviour
    intact rather than publish the frame on an empty channel.
    """
    block = _anchor_block()
    seed = block.index("row_id = self.context_id")
    assert seed < block.index("try:")
