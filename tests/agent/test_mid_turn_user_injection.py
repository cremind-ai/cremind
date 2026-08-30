"""Folding a mid-turn user message into the running reasoning loop.

The message becomes a real ``role:"user"`` message, drained at the TOP of a
step, before the messages array is assembled, so it is always carried by the
very next LLM call. (At the bottom of the loop the max-steps exit could consume
one into the persisted trace without any call ever seeing it.)

It also fires inside event runs, which the task-result notice deliberately skips:
a reply to a running event run is exactly the case this exists for. The notice's
own half of the shared drain/break/ack path lives in test_event_result_notice.py.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.agent.reasoning_agent import ReasoningAgent  # noqa: E402
from app.events import task_result_inbox  # noqa: E402
from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.utils.task_context import current_task_id_var  # noqa: E402

_RUN = "msg:conv-1:abc"
_CONV = "conv-1"


@pytest.fixture(autouse=True)
def _clean():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _agent(*, event_run: bool = False) -> ReasoningAgent:
    """A skeleton with just the attributes the drain path touches."""
    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._turn_messages = []
    agent._tool_result_enabled = True
    agent._tool_result_max_tokens = 4000
    agent._event_run = event_run
    agent.current_step_count = 1
    return agent


def _park(text: str, *, agent_text: str | None = None, mid: str = "m1"):
    task_result_inbox.park_user_message_if_bound(_CONV, {
        "message_id": mid, "text": text, "agent_text": agent_text or text,
    })


def _drain(agent: ReasoningAgent) -> list[dict]:
    token = current_task_id_var.set(_RUN)
    try:
        return agent._drain_user_messages()
    finally:
        current_task_id_var.reset(token)


def test_a_parked_message_becomes_a_user_message():
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("actually, use the staging DB")

    out = _drain(_agent())

    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "actually, use the staging DB" in out[0]["content"]


def test_nothing_parked_yields_nothing():
    task_result_inbox.bind_run(_RUN, _CONV)
    assert _drain(_agent()) == []


def test_a_burst_is_one_coalesced_message():
    """The model reads a step's input as a whole; N messages is not N turns."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("wait", mid="m1")
    _park("use staging", mid="m2")
    _park("and skip the migration", mid="m3")

    out = _drain(_agent())

    assert len(out) == 1
    content = out[0]["content"]
    assert "wait" in content and "use staging" in content
    assert "and skip the migration" in content
    assert "3 new messages" in content


def test_the_agent_sees_the_resolved_text_not_the_raw_row():
    """The persisted row keeps `$VAR` chips; the model gets the rendered text."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("deploy to $ENV", agent_text="deploy to production")

    content = _drain(_agent())[0]["content"]

    assert "deploy to production" in content
    assert "$ENV" not in content


def test_it_reads_correctly_when_replayed_in_a_later_turn():
    """This text is persisted in the turn's trace and replayed later, so it must
    not claim anything that has since become false."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("hold on")

    content = _drain(_agent())[0]["content"].lower()

    # Past tense about the send, and explicit that it will not come again — a
    # model reading this later must not sit waiting for a duplicate turn.
    assert "was already in progress" in content
    assert "will not arrive again" in content
    assert "you are currently" not in content


def test_the_wrapper_does_not_also_ask_for_an_immediate_reply():
    """_acknowledge_interruption owns the immediate reply. Asking here too
    invites a second answer in the very next tool step."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("Have you finished installing?")

    content = _drain(_agent())[0]["content"].lower()

    assert "right away" not in content
    assert "answer briefly" not in content
    # What it DOES carry: act on a change now, and cover it at the end.
    assert "act on that from here" in content
    assert "final answer must address" in content


def test_the_wrapper_agrees_with_itself_on_number():
    """One message reads 'it', a burst reads 'they' — pronouns AND verbs. The
    instructions are read as prose, and 'if they changes' is the kind of wrong
    that makes a model hesitate about what it is being told."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("one", mid="m1")
    single = _drain(_agent())[0]["content"]
    assert "take it into account" in single
    assert "it will not arrive again" in single
    assert "if it changes or cancels what was being done" in single.lower()
    assert "must address it." in single

    _park("one", mid="m2")
    _park("two", mid="m3")
    burst = _drain(_agent())[0]["content"]
    assert "take them into account" in burst
    assert "they will not arrive again" in burst
    assert "if they change or cancel what was being done" in burst.lower()
    assert "must address them." in burst


def test_event_runs_are_included():
    """A reply to a running event run is the whole point — unlike the task
    notice, this channel is NOT suppressed there."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("cancel that, wrong repo")

    agent = _agent(event_run=True)
    assert len(_drain(agent)) == 1
    # And the task-result notice stays suppressed on the same agent.
    task_result_inbox.park_if_bound(_CONV, {"label": "CI", "status_word": "done"})
    token = current_task_id_var.set(_RUN)
    try:
        assert agent._drain_task_notices() == []
    finally:
        current_task_id_var.reset(token)


def test_drain_is_once_per_message():
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("only once")
    agent = _agent()

    assert len(_drain(agent)) == 1
    assert _drain(agent) == []


def test_outside_a_bound_run_it_is_a_no_op():
    _park("nobody is listening")   # never parked: no binding
    assert _drain(_agent()) == []


def test_the_drain_happens_before_the_messages_array_is_built():
    """Structural: a drain below the LLM call could be consumed into the
    persisted trace by the max-steps exit without any call carrying it."""
    src = inspect.getsource(ReasoningAgent._loop)
    drain_at = src.index("_drain_user_messages()")
    assemble_at = src.index('{"role": "system", "content": instruction}')
    assert drain_at < assemble_at


def test_injected_messages_land_in_the_persisted_trace():
    """_build_llm_messages must carry them, since the commit hook treats a
    persisted trace as proof the injection is durable."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("one more thing")
    agent = _agent()
    agent._turn_messages.extend(_drain(agent))

    built = ReasoningAgent._build_llm_messages(agent, "here you go")

    assert built is not None
    assert any(
        m.get("role") == "user" and "one more thing" in str(m.get("content"))
        for m in built
    )
    # And the answer that addressed it closes the trace.
    assert built[-1] == {"role": "assistant", "content": "here you go"}


# ── the immediate acknowledgement ───────────────────────────────────────────
#
# A tool-calling model treats "text" as "the turn is over", so told to both
# speak and keep working it just keeps working — measured at 0/6 on the
# configured model with the wording alone. So the reply gets its own call with
# tools withheld, which is the only thing it can answer with words.


class _FakeLLM:
    """Captures the ack call and replays a scripted reply."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[dict] = []

    async def chat_completion_stream(self, **kwargs):
        self.calls.append(kwargs)
        for piece in [self.reply]:
            if piece:
                yield {"type": ChatCompletionTypeEnum.CONTENT, "data": piece}
        yield {"type": ChatCompletionTypeEnum.DONE, "finish_reason": "stop"}


def _ack_agent(reply: str) -> ReasoningAgent:
    agent = _agent()
    agent.llm = _FakeLLM(reply)
    agent.history_messages = []
    agent._current_query = "install openclaw"
    agent._reasoning_temperature = 0.2
    agent._enable_prompt_cache = True
    agent._accumulate_tokens = lambda resp: None
    agent._record_reasoning_usage = lambda resp: None
    agent._render_input = lambda: "install openclaw"
    agent._pending_ack_text = ""
    return agent


async def _collect_ack(agent, request: str | None = None) -> list:
    from app.agent.reasoning_agent import _ACK_REQUEST

    return [
        c async for c in agent._acknowledge_interruption(
            "SYSTEM", request if request is not None else _ACK_REQUEST,
        )
    ]


def test_the_ack_reaches_the_user_and_joins_the_turn():
    agent = _ack_agent("Not yet — 2 of 4 steps done.")
    chunks = asyncio.run(_collect_ack(agent))

    assert [c["data"] for c in chunks] == ["Not yet — 2 of 4 steps done.\n\n"]
    # Held for the next assistant message rather than appended as one of its
    # own — two assistant turns back to back is a trace shape not every provider
    # accepts on replay.
    assert agent._pending_ack_text == "Not yet — 2 of 4 steps done."
    assert agent._turn_messages == []


def test_the_ack_call_withholds_tools():
    """The whole point: with tools on the table the model reaches for one
    instead of speaking."""
    agent = _ack_agent("Still going.")
    asyncio.run(_collect_ack(agent))

    call = agent.llm.calls[0]
    assert call["tools"] is None
    assert call["tool_choice"] is None
    assert call["max_tokens"] == 200


def test_the_ack_sees_the_whole_turn_so_far():
    """It is the same model on the same context — that is what lets it say
    'two of four done' instead of a canned 'still working'."""
    agent = _ack_agent("2 of 4 done.")
    agent._turn_messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "exec_shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "installed-curl"},
    ]
    asyncio.run(_collect_ack(agent))

    sent = agent.llm.calls[0]["messages"]
    assert sent[0] == {"role": "system", "content": "SYSTEM"}
    assert any(m.get("content") == "installed-curl" for m in sent)
    assert sent[-1]["role"] == "user" and "SKIP" in sent[-1]["content"]


@pytest.mark.parametrize("reply", ["SKIP", "skip", "**SKIP**", "SKIP.", "   "])
def test_skip_keeps_the_agent_silent(reply):
    """Its discretion: deep in an important flow, report later instead."""
    agent = _ack_agent(reply)
    chunks = asyncio.run(_collect_ack(agent))

    assert chunks == []
    assert agent._pending_ack_text == ""
    assert agent._turn_messages == []


def test_a_failed_ack_never_breaks_the_turn():
    class _Boom:
        async def chat_completion_stream(self, **kwargs):
            raise RuntimeError("provider down")
            yield  # pragma: no cover

    agent = _ack_agent("unused")
    agent.llm = _Boom()

    assert asyncio.run(_collect_ack(agent)) == []
    assert agent._pending_ack_text == ""
    assert agent._turn_messages == []


def test_the_ack_is_folded_into_the_next_assistant_message():
    """Structural: the step seeds assistant_parts with the pending ack, so the
    trace carries ONE assistant turn that both spoke and acted — never two in a
    row, which some providers reject on replay."""
    src = inspect.getsource(ReasoningAgent._loop)
    seed = src.index("_pending_ack_text")
    assert "assistant_parts.append(self._pending_ack_text" in src
    # Seeded before the LLM call that builds the message, and cleared on use.
    assert seed < src.index("chat_completion_stream")
    assert 'self._pending_ack_text = ""' in src


def test_the_ack_fires_only_when_something_was_injected():
    """Structural: the extra call is paid for only when something arrives."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "if injected:" in src
    assert src.index("_drain_user_messages()") < src.index("_acknowledge_interruption")
    assert src.index("if injected:") < src.index("_acknowledge_interruption")


def test_a_user_message_and_a_task_notice_merge_into_one_message():
    """Both landed during the same step. Two role:"user" messages back to back
    is a trace shape not every provider accepts on replay, and the model reads
    a step's input as a whole anyway."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("are we there yet?")
    task_result_inbox.park_if_bound(_CONV, {"label": "CI", "status_word": "completed"})
    agent = _agent()

    token = current_task_id_var.set(_RUN)
    try:
        drained = agent._drain_user_messages()
        notices = agent._drain_task_notices()
    finally:
        current_task_id_var.reset(token)

    # What the loop then does with them.
    assert len(drained) == 1 and len(notices) == 1
    src = inspect.getsource(ReasoningAgent._loop)
    assert 'drained[0]["content"] += "\\n\\n" + notices[0]["content"]' in src
    assert "injected = drained + notices" in src


def test_the_user_message_owns_the_ack_wording_when_both_arrive():
    """Structural: a person asking a question outranks a task landing — the
    reply has to answer them, and the notice rides along in the same block."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "request = _ACK_REQUEST if drained else _TASK_ACK_REQUEST" in src


# ── the flow break ──────────────────────────────────────────────────────────
#
# One continuous chain reaches the model; the UI shows the turn as the bubbles
# the user watched arrive. The marker is what carries that split — live as a bus
# frame, and (via the runner's offsets) through a reload.


def test_the_drain_records_the_row_ids_it_covers():
    """The UI pairs these with the break to put the bubbles back between the
    same two stretches of work they interrupted."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("first", mid="m1")
    _park("second", mid="m2")
    agent = _agent()

    _drain(agent)

    assert agent._drained_message_ids == ["m1", "m2"]


def test_ids_missing_from_a_payload_are_skipped_not_nulled():
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(
        _CONV, {"text": "no id", "agent_text": "no id"},
    )
    agent = _agent()

    _drain(agent)

    assert agent._drained_message_ids == []


def _break_block() -> str:
    """The `if drained or ack_chunks:` body — everything the step emits."""
    src = inspect.getsource(ReasoningAgent._loop)
    block = src[src.index("if drained or ack_chunks:"):]
    return block[: block.index("chat_completion_stream")]


def test_the_break_is_emitted_before_the_reply():
    """Order is load-bearing: the runner stamps the content offset when the
    marker passes, so a reply emitted afterwards belongs to the NEW segment.
    Reversed, the reply would trail the work it interrupted.

    Note the ack CALL happens earlier — it is buffered, and nothing is emitted
    until the decision below. What matters is the order things are yielded in.
    """
    block = _break_block()
    assert block.index("FLOW_BREAK") < block.index("for ack in ack_chunks:")


def test_the_break_carries_the_ids_and_the_step():
    block = _break_block()
    marker = block[: block.index("for ack in ack_chunks:")]
    assert "self._drained_message_ids" in marker
    assert "self.current_step_count" in marker


def test_a_user_message_breaks_the_flow_even_when_the_agent_stays_silent():
    """SKIP means it had nothing useful to say yet — the message still arrived,
    and the work that follows still belongs under its bubble rather than above.
    That is what `drained or` buys: the ids gate the break on their own."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "if drained or ack_chunks:" in src


def test_a_silent_task_result_leaves_the_flow_unbroken():
    """It has no bubble of its own, so a break the agent did not speak into
    would split the thinking panel with nothing to explain the split."""
    src = inspect.getsource(ReasoningAgent._loop)
    # Reachable only via ack_chunks when nothing was drained.
    assert "if drained or ack_chunks:" in src
    assert "if injected:" in src
    # And the ack is collected before that decision, not streamed through it.
    assert src.index("ack_chunks = [") < src.index("if drained or ack_chunks:")


def test_a_spoken_reply_is_closed_off_as_its_own_message():
    """A SECOND break right after the reply, so it reads as its own bubble
    instead of the final answer growing out of it. Gated on the reply existing
    — SKIP must not leave an empty bubble behind."""
    block = _break_block()
    replay_at = block.index("for ack in ack_chunks:")
    gate_at = block.index("if ack_chunks:")
    assert replay_at < gate_at
    # It interleaves no rows: the reply's own bubble is its whole visible trace.
    assert '"message_ids": []' in block[gate_at:]


def test_normalize_keeps_a_trailing_injected_message():
    """It sits after a fully-answered tool group, so the trailing-unanswered
    truncation must not eat it."""
    agent = _agent()
    agent._turn_messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
        {"role": "user", "content": "[New message from the user] hi"},
    ]

    kept = ReasoningAgent._normalize_turn_messages(agent._turn_messages)

    assert kept[-1]["role"] == "user"


# ── declining a question that was routed here ──────────────────────────────


def test_a_declined_answer_to_a_routed_message_is_asked_again():
    """Structural, and the reason the whole retry exists.

    The first request lets the agent decline, and against the configured model
    that decision does not hold still: measured on the exact array from a
    reported failure, every wording tried answered 4/6-6/6 in a replica while
    the live turns answered 0/8. Where the room has already routed the message
    to this agent by name, "not mine" is not one of the reasons it could have
    had — so it is asked once more, without the option (16/16 measured).
    """
    src = inspect.getsource(ReasoningAgent._loop)
    assert "_GROUP_ACK_INSIST" in src
    # Only after the first request came back empty...
    first = src.index("_GROUP_ACK_REQUEST")
    insist = src.index("_GROUP_ACK_INSIST")
    assert first < insist
    assert "if room and not ack_chunks" in src
    # ...and only for a message the room routed here.
    assert "_drained_addressed" in src


def test_the_insisting_request_offers_no_way_out():
    """A second request that still says "or answer SKIP" would just be the
    first one again."""
    text = ra._GROUP_ACK_INSIST
    assert "do not answer SKIP" in text
    assert "ONE short sentence" in text
    # It says an unfinished answer is a real answer — the failure mode it is
    # replacing was the model treating "the job isn't done" as nothing to say.
    assert "even if the work is unfinished" in text


def test_the_retry_never_fires_in_a_one_to_one_chat():
    """There is no router in a DM: `room` gates it, and _ACK_REQUEST's own SKIP
    stays a real choice there."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "room = drained and getattr(self, \"_room_chat\", False)" in src


def test_the_drain_records_whether_the_room_routed_it_here():
    """Read off the parked payload, where the router's verdict was recorded —
    not re-derived from the text, which would be a second guess at a question
    already answered."""
    src = inspect.getsource(ReasoningAgent._drain_user_messages)
    assert 'self._drained_addressed = any(p.get("addressed") for p in parked)' in src
