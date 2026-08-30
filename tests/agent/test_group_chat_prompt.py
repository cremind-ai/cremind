"""The group-chat section of the system prompt, and the wording that goes with it.

A seat in a group is a different social situation from a private chat, and the
prompt is where that difference is taught. Three things have to hold:

* the block describes the room concretely — who is in it, whose word counts as
  an instruction, and how to decline (the ``[silent]`` sentinel), because
  "answer only if it is for you" is useless advice without a way to say nothing;
* it appears ONLY for a seat: every other conversation must render exactly the
  prompt we shipped before group chats existed, or the cached prefix that every
  ordinary turn shares would fragment;
* the mid-turn wording flips with it. Outside a group an interruption is by
  definition for you; inside one it usually is not.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.agent.reasoning_agent import ReasoningAgent  # noqa: E402


class _FakeTool:
    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id


class _FakeRegistry:
    def __init__(self, tools) -> None:
        self._tools = tools

    def tools_for_profile(self, profile):
        return list(self._tools)


def _fake_agent_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_llm_retries=0,
        reasoning_temperature=1.0,
        reasoning_max_tokens=1024,
        reasoning_retry=0,
        tool_result_enabled=False,
        tool_result_max_tokens=4096,
        enable_prompt_cache=False,
        max_steps=6,
    )


def _build(monkeypatch, *, message_origin=None, profile="dog"):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile=profile, context_id="ctx",
        message_origin=message_origin,
    )


GROUP_ORIGIN = {
    "source": "group_chat",
    "group_id": "g-1",
    "group_name": "Morning Ops",
    "self_profile": "dog",
    "self_name": "Dog",
    "members": [
        {"profile": "dog", "agent_name": "Dog"},
        {"profile": "cat", "agent_name": "Cat"},
        {"profile": "chicken", "agent_name": "Chicken"},
    ],
    "max_agent_hops": 6,
}


# ── the block ──────────────────────────────────────────────────────────────


def test_the_block_names_the_room_and_the_agent():
    block = ra._format_message_origin_block(GROUP_ORIGIN)
    assert "GROUP CHAT" in block
    assert "Morning Ops" in block
    assert "You are Dog" in block


def test_the_roster_lists_every_member_by_name():
    """Read off the roster LINE rather than the whole block: "Dog" also appears
    in "You are Dog", so a substring search would pass on a roster that had
    silently lost the agent it was meant to list."""
    block = ra._format_message_origin_block(GROUP_ORIGIN)
    members = next(line for line in block.splitlines() if line.startswith("- Members:"))
    # The agent has to be able to find itself among its peers.
    assert members == "- Members: Dog — you; Cat; Chicken"


def test_a_person_outranks_a_peer_when_the_two_conflict():
    """The room is agents talking to agents most of the time, so "another
    member asked me to" has to be weaker than "a person asked me to" — or one
    agent's mistaken instruction propagates round the whole room."""
    block = ra._format_message_origin_block(GROUP_ORIGIN)
    assert "a person's request is an instruction" in block
    assert "not an instruction" in block
    assert "follow the person" in block


def test_it_teaches_the_silence_sentinel_exactly():
    block = ra._format_message_origin_block(GROUP_ORIGIN)
    assert "[silent]" in block
    assert "ENTIRE answer must be exactly" in block
    # A bare "ok" from three agents is the failure mode this line prevents.
    assert "bare acknowledgement" in block


def test_it_explains_the_attribution_prefix_and_forbids_repeating_it():
    block = ra._format_message_origin_block(GROUP_ORIGIN)
    assert "(user)" in block and "(agent)" in block
    assert "Never write that prefix yourself" in block


def test_a_room_with_no_roster_yet_still_renders():
    """The members are read from the group row, so a race with a membership
    change can hand this an empty list; the block must not collapse into
    something that reads as 'you are alone in here'."""
    block = ra._format_message_origin_block({**GROUP_ORIGIN, "members": []})
    assert "GROUP CHAT" in block
    assert "- Members:" not in block
    assert "[silent]" in block


# ── isolation from every other conversation ────────────────────────────────


def test_other_origins_are_untouched():
    web = ra._format_message_origin_block({"source": "web_ui"})
    assert "MESSAGE SOURCE" in web and "GROUP CHAT" not in web
    channel = ra._format_message_origin_block({
        "source": "channel", "channel_id": "c1", "channel_type": "telegram",
        "channel_name": "Telegram", "sender_id": "1", "sender_display_name": "Lee",
    })
    assert "MESSAGE SOURCE" in channel and "GROUP CHAT" not in channel
    assert ra._format_message_origin_block(None) == ""
    assert ra._format_message_origin_block({"source": "mystery"}) == ""


def test_a_prompt_without_an_origin_is_unchanged(monkeypatch):
    """The bare prompt is the cached prefix every ordinary turn shares."""
    agent = _build(monkeypatch, message_origin=None)
    bare = agent._build_instruction()
    assert "GROUP CHAT" not in bare
    assert re.search(r"Your name: .*\n\nYou are a capable assistant\.", bare)


def test_the_block_reaches_the_prompt_and_is_frozen(monkeypatch):
    agent = _build(monkeypatch, message_origin=GROUP_ORIGIN)
    first = agent._build_instruction()
    assert "GROUP CHAT" in first
    # Rendered once in __init__, so every step of the run sends the same bytes.
    assert agent._build_instruction() == first


def test_the_group_flag_follows_the_origin(monkeypatch):
    assert _build(monkeypatch, message_origin=GROUP_ORIGIN)._group_chat is True
    assert _build(monkeypatch, message_origin={"source": "web_ui"})._group_chat is False
    assert _build(monkeypatch, message_origin=None)._group_chat is False


def test_the_flag_has_a_class_level_default():
    """Prompt-only tests build the agent with __new__; a missing attribute there
    would make every group branch raise instead of falling back."""
    assert ReasoningAgent._group_chat is False


# ── mid-turn wording ───────────────────────────────────────────────────────


def _drain_agent(*, group_chat: bool) -> ReasoningAgent:
    agent = ReasoningAgent.__new__(ReasoningAgent)
    # Both flags: a seat IS a room, and the two are set together in ``__init__``.
    # The fold wording keys off ``_room_chat`` (a platform group needs the same
    # words), while ``_group_chat`` stays seat-only because it gates the tool.
    agent._group_chat = group_chat
    agent._room_chat = group_chat
    agent._drained_message_ids = []
    return agent


def _folded(agent, parked):
    from app.events import task_result_inbox
    from app.utils.task_context import current_task_id_var

    task_result_inbox.clear_all()
    task_result_inbox.bind_run("run-1", "conv-1")
    for payload in parked:
        task_result_inbox.park_user_message_if_bound("conv-1", payload)
    token = current_task_id_var.set("run-1")
    try:
        return agent._drain_user_messages()
    finally:
        current_task_id_var.reset(token)
        task_result_inbox.clear_all()


def test_the_group_fold_says_the_message_may_not_be_yours():
    agent = _drain_agent(group_chat=True)
    out = _folded(agent, [{"message_id": "m1", "agent_text": "Cat (agent): done"}])
    content = out[0]["content"]
    assert "new group message" in content
    assert "not addressed to you" in content
    assert "you do not owe" in content
    # The private chat's unconditional obligation must NOT appear here — in a
    # room most of what arrives is somebody else's.
    assert "The final answer must address" not in content


def test_the_group_fold_still_owes_an_answer_to_what_was_for_it():
    """The other half of "you do not owe it a reply".

    Not owing an answer is about other people's traffic. A message that WAS
    addressed to this agent cannot be dropped just because the pause turned out
    to be a poor moment to answer it — that is what left an agent mute through
    "answer me now" and then never coming back to it.
    """
    agent = _drain_agent(group_chat=True)
    content = _folded(
        agent, [{"message_id": "m1", "agent_text": "Hà: answer me now"}],
    )[0]["content"]
    assert "addressed to you that you have not already answered" in content
    assert "final message" in content


def test_the_private_fold_is_unchanged():
    agent = _drain_agent(group_chat=False)
    content = _folded(
        agent, [{"message_id": "m1", "agent_text": "actually, stop"}],
    )[0]["content"]
    assert "[New message from the user" in content
    assert "The final answer must address it.]" in content


def test_the_group_fold_agrees_with_itself_on_number():
    agent = _drain_agent(group_chat=True)
    content = _folded(agent, [
        {"message_id": "m1", "agent_text": "one"},
        {"message_id": "m2", "agent_text": "two"},
    ])[0]["content"]
    assert "2 new group messages" in content
    assert "they are not addressed to you" in content.lower()


def test_the_group_ack_request_asks_for_an_answer_before_offering_an_out():
    """Order is the whole fix, twice over.

    Two wordings failed live before this one, both because they put the decision
    first: one ordered silence outright ("your final answer will speak for it"),
    the next opened with "answer it now only if it is for you and cannot wait"
    plus "Judge that for yourself". Against gpt-5.4-mini the hedge won — SKIP,
    five output tokens, to "have you finished installing? [to: you]". So the
    instruction leads and the exceptions come last, exactly like the one-to-one
    request that has always worked.
    """
    text = ra._GROUP_ACK_REQUEST
    assert "Answer it in ONE short sentence" in text
    assert text.index("Answer it in ONE short sentence") < text.index("SKIP")
    # None of the hedges that lost.
    for hedge in ("only if it is for you", "Judge that for yourself",
                  "cannot wait", "your final answer will speak for it"):
        assert hedge not in text
    src = inspect.getsource(ReasoningAgent._loop)
    assert "_GROUP_ACK_REQUEST" in src
    # The existing precedence (a person outranks a task result) is untouched.
    assert "request = _ACK_REQUEST if drained else _TASK_ACK_REQUEST" in src


def test_the_group_ack_request_leans_on_the_routing_already_done():
    """It does not re-ask "is this mine?" — nothing that was not already routed
    to this agent can reach the pause. A room post routed elsewhere is
    quiet-written and never parked; a platform-group message parks only after
    the relevance gate."""
    assert "It was routed to you." in ra._GROUP_ACK_REQUEST


def test_the_group_ack_request_keeps_the_agents_discretion():
    """The user asked for this explicitly: an important task may be finished
    first. What is gone is the *default* to defer, not the option."""
    text = ra._GROUP_ACK_REQUEST
    assert "Respond with exactly SKIP only if" in text
    assert "for another member" in text
    assert "already answered it" in text
    assert "too early to say anything useful" in text
    # Deferring is allowed; forgetting is not.
    assert "your final message must cover it" in text


def test_the_pause_reply_is_exempted_from_the_silence_rule():
    """Both room kinds argue for silence in the same system prompt the ack call
    rides. Without this the model reads speaking at the pause as breaking a
    rule it was just given — and a mini model resolves that by not speaking."""
    for origin in (
        {"source": "group_chat", "group_name": "Ops", "self_profile": "dog",
         "agent_name": "Rex", "members": []},
        {"source": "channel_group", "group_title": "AI-gr",
         "channel_type": "zalo", "members": []},
    ):
        block = ra._format_message_origin_block(origin) or ""
        assert "INTERRUPTIONS:" in block
        assert "judged on its own" in block
        assert "[silent]" in block
