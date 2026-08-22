"""The tool the agent calls to pull results it was told are waiting.

Thin by design — the claim discipline lives in ``event_task_delivery`` — so what
matters here is the wiring: it must find its conversation through the run
binding (``_context_id`` is not the conversation id on channel-backed chats), it
must be hidden and withheld from event runs, and an empty inbox must read as
"nothing to do", never as a failure the model tries to work around.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

from app.events import task_result_inbox  # noqa: E402
from app.tools.base import make_leaf_name  # noqa: E402
from app.tools.builtin import _BUILTIN_MODULE_NAMES  # noqa: E402
from app.tools.builtin.get_event_task_results import (  # noqa: E402
    TOOL_CONFIG, GetEventTaskResultsTool, get_tools,
)
from app.utils.task_context import current_task_id_var  # noqa: E402

_RUN = "msg:conv-1:abc"
_CONV = "conv-1"


@pytest.fixture(autouse=True)
def _clean():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _call(tool, **args):
    token = current_task_id_var.set(_RUN)
    try:
        return asyncio.run(tool.run({"_profile": "p1", **args}))
    finally:
        current_task_id_var.reset(token)


def _text(result) -> str:
    return "".join(part.get("text", "") for part in result.content)


# ── registration + shape ────────────────────────────────────────────────────


def test_it_is_registered_and_hidden():
    assert "get_event_task_results" in _BUILTIN_MODULE_NAMES
    assert TOOL_CONFIG["hidden"] is True


def test_the_model_sees_a_single_clean_function_name():
    """Leaf name == tool id, so make_leaf_name collapses the doubled form."""
    assert make_leaf_name("get_event_task_results", "get_event_task_results") == (
        "get_event_task_results"
    )


def test_it_takes_no_arguments():
    """One call takes everything waiting.

    A per-id filter would let the agent read one result and strand another,
    which buys nothing: its discretion is about WHEN to read, not which.
    """
    tool = get_tools({})[0]
    assert tool.parameters["properties"] == {}
    assert tool.parameters["additionalProperties"] is False


def test_the_description_tells_the_model_ignoring_the_notice_is_safe():
    """Hidden tools get no catalogue entry, so this text is the only cue."""
    desc = GetEventTaskResultsTool.description
    assert "no arguments" in desc
    assert "EVERY result" in desc
    assert "delivered automatically as a new turn" in desc


# ── behaviour ───────────────────────────────────────────────────────────────


def test_outside_a_live_turn_it_reports_nothing_waiting(monkeypatch):
    """No binding (e.g. the legacy executor path) is not an error."""
    called = []

    async def _never(**kwargs):
        called.append(kwargs)
        return ("", [])

    import app.events.event_task_delivery as etd
    monkeypatch.setattr(etd, "read_origin_inbox", _never)

    out = _text(_call(get_tools({})[0]))
    assert "nothing waiting" in out.lower() or "only available" in out.lower()
    assert called == [], "it must not query an inbox it cannot identify"


def test_it_resolves_its_conversation_through_the_run_binding(monkeypatch):
    seen = {}

    async def _read(*, conversation_id, profile):
        seen["conversation_id"] = conversation_id
        seen["profile"] = profile
        return ("[Event task results — 1 ready]\n\nAwaited: CI\n", [3])

    import app.events.event_task_delivery as etd
    monkeypatch.setattr(etd, "read_origin_inbox", _read)
    task_result_inbox.bind_run(_RUN, _CONV)

    out = _text(_call(get_tools({})[0]))
    assert seen == {"conversation_id": _CONV, "profile": "p1"}
    assert "Awaited: CI" in out


def test_reading_carries_the_chain_depth_into_this_turn(monkeypatch):
    """Otherwise wait → read → register → wait escapes the runaway cap.

    A pulled result mints no trigger event, so the depth would restart at zero
    on every hop and the chain could run forever.
    """
    async def _read(*, conversation_id, profile):
        return ("results", [2, 7, 4])

    import app.events.event_task_delivery as etd
    monkeypatch.setattr(etd, "read_origin_inbox", _read)
    task_result_inbox.bind_run(_RUN, _CONV)

    _call(get_tools({})[0])
    assert task_result_inbox.consumed_depth(_RUN) == 7, "the deepest wins"


def test_nothing_is_recorded_when_the_inbox_was_empty(monkeypatch):
    async def _read(*, conversation_id, profile):
        return ("No task results are waiting.", [])

    import app.events.event_task_delivery as etd
    monkeypatch.setattr(etd, "read_origin_inbox", _read)
    task_result_inbox.bind_run(_RUN, _CONV)

    _call(get_tools({})[0])
    assert task_result_inbox.consumed_depth(_RUN) == 0
