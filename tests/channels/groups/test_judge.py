"""The relevance judge — what it is asked, and which way it fails.

Two separate things, and an earlier version of this feature conflated them.

*Ambiguity* resolves towards answering. The agent is a member of the group, not
an intruder in it: a question put to everyone is put to it too. Judging
otherwise made it sit out "Hello everyone, how are you?" while every human
member answered.

*Failure* resolves towards silence. No tool call, unreadable arguments, a
timeout, no LLM at all — those stay quiet, because a provider outage must not
turn a group into a chatterbox.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.channels.groups.judge import classify_relevance, judge_relevance
from app.channels.groups.render import render_recent_for_judge
from app.constants import ChatCompletionTypeEnum


class _FakeLLM:
    """One tool-calling completion, scripted."""

    provider_name = "fake"
    model_name = "fake-mini"

    def __init__(self, *, function_calls=None, tokens=None):
        self._function_calls = function_calls
        self._tokens = tokens or {
            "input_tokens": 10, "output_tokens": 2,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        self.seen_messages = None

    async def chat_completion(self, *, messages, tools, tool_choice):
        self.seen_messages = messages
        if self._function_calls is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": self._function_calls},
            }
        yield {"type": ChatCompletionTypeEnum.DONE, **self._tokens}


def _call(llm, message="anyone seen the deploy?", **overrides):
    kwargs = dict(
        llm=llm,
        agent_name="Rex",
        agent_handle="@opsbot",
        account_name="Rex Nguyen",
        platform_name="Telegram",
        group_title="Ops room",
        members=["Alexa Nguyen", "Sam"],
        recent=["Alexa (user): morning"],
        message=message,
    )
    kwargs.update(overrides)
    return asyncio.run(classify_relevance(**kwargs))


def _tool(args):
    return [{"name": "report_relevance", "arguments": args}]


def test_a_yes_is_a_yes():
    result = _call(_FakeLLM(function_calls=_tool(
        {"relevant": True, "reason": "asks about the deploy Rex reported"},
    )))
    assert result.relevant is True
    assert "deploy" in result.reason
    assert result.tokens["input_tokens"] == 10


def test_a_no_is_a_no():
    result = _call(_FakeLLM(function_calls=_tool(
        {"relevant": False, "reason": "two other people talking"},
    )))
    assert result.relevant is False


def test_arguments_that_arrive_as_a_json_string_still_parse():
    """Providers differ on whether the arguments come back parsed."""
    result = _call(_FakeLLM(function_calls=_tool(
        json.dumps({"relevant": True, "reason": "addressed directly"}),
    )))
    assert result.relevant is True


@pytest.mark.parametrize("function_calls", [
    None,                                          # no tool call at all
    _tool({"reason": "hmm"}),                      # no verdict
    _tool({"relevant": "perhaps", "reason": ""}),  # unparseable verdict
    _tool("not json at all"),                      # unparseable arguments
])
def test_anything_it_cannot_read_means_stay_quiet(function_calls):
    """Fail CLOSED. A junk answer must never coerce to "yes" — the cost lands in
    a group of real people, not in a log."""
    assert _call(_FakeLLM(function_calls=function_calls)).relevant is False


def test_the_prompt_carries_the_thread_and_the_new_message():
    """Without the recent messages the judge cannot tell an exchange the agent
    is already in from two other people talking, which is most of the job."""
    llm = _FakeLLM(function_calls=_tool({"relevant": False, "reason": "no"}))
    _call(llm, message="Alexa (user): and the rollback?")

    user_turn = llm.seen_messages[-1]["content"]
    assert "Ops room" in user_turn
    assert "Telegram" in user_turn
    assert "Rex" in user_turn and "@opsbot" in user_turn
    assert "Alexa (user): morning" in user_turn
    assert "and the rollback?" in user_turn


# ── the resolving wrapper ─────────────────────────────────────────────────


def test_no_agent_wired_means_stay_quiet(monkeypatch):
    """The CLI and the tests run with no agent. Answering "yes" there would make
    a misconfigured install the chattiest one."""
    import app.events.runner as runner

    monkeypatch.setattr(runner, "get_cremind_agent", lambda: None)
    assert asyncio.run(_judge()) is False


def test_a_provider_error_means_stay_quiet(monkeypatch):
    _wire_llm(monkeypatch, _Exploding())
    assert asyncio.run(_judge()) is False


def test_a_timeout_means_stay_quiet(monkeypatch):
    _wire_llm(monkeypatch, _Hanging())
    import app.channels.groups.judge as judge_mod

    monkeypatch.setattr(judge_mod, "_TIMEOUT_S", 0.01)
    assert asyncio.run(_judge()) is False


def test_a_verdict_is_billed_to_the_group_conversation(monkeypatch):
    """The cost belongs where the messages that caused it are."""
    _wire_llm(monkeypatch, _FakeLLM(function_calls=_tool(
        {"relevant": True, "reason": "asked directly"},
    )))
    records: list = []

    class _Usage:
        async def add_usage_records(self, **kw):
            records.append(kw)

    import app.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_usage_storage", lambda *a, **k: _Usage())

    assert asyncio.run(_judge(conversation_id="conv-9")) is True
    (call,) = records
    assert call["conversation_id"] == "conv-9"
    assert call["profile"] == "admin"
    (record,) = call["records"]
    assert record["source_kind"] == "group_judge"
    # ``usage_records.source_kind`` is String(16): anything longer is truncated
    # on SQLite and rejected on Postgres.
    assert len(record["source_kind"]) <= 16


# ── what it is told ───────────────────────────────────────────────────────


def test_it_is_told_the_name_the_group_addresses_the_agent_by():
    """The agent's name inside Cremind is not the name on its account. Without
    the account name the judge reads "Rex Nguyen, what time is it?" as a message
    for a third party and declines to answer on the agent's own behalf."""
    llm = _FakeLLM(function_calls=_tool({"relevant": True, "reason": "asked"}))
    _call(llm)
    prompt = llm.seen_messages[1]["content"]
    assert "Rex Nguyen" in prompt


def test_it_is_told_who_else_is_in_the_group():
    """"Addressed to somebody else by name" is only decidable against a list of
    the names."""
    llm = _FakeLLM(function_calls=_tool({"relevant": False, "reason": "no"}))
    _call(llm)
    prompt = llm.seen_messages[1]["content"]
    assert "Alexa Nguyen" in prompt and "Sam" in prompt


def test_a_message_to_the_whole_group_is_a_message_to_the_agent():
    """The rule the first version got backwards, pinned in the instructions the
    model actually reads."""
    llm = _FakeLLM(function_calls=_tool({"relevant": True, "reason": "group"}))
    _call(llm)
    system = llm.seen_messages[0]["content"].lower()
    assert "addressed to the group as a whole" in system
    assert "relevant=true" in system
    # And the tie-break is towards answering, not away from it.
    assert "aimed at the group as a whole, answer relevant=true" in system


def test_it_is_warned_off_answering_another_assistant():
    """Two agents in one group answering each other is the failure mode the
    brakes exist for; the judge is the cheaper place to stop it."""
    llm = _FakeLLM(function_calls=_tool({"relevant": False, "reason": "bot"}))
    _call(llm)
    assert "another assistant" in llm.seen_messages[0]["content"].lower()


# ── the transcript it reads ───────────────────────────────────────────────


def test_the_agents_own_turns_are_labelled_and_its_silences_are_not_shown():
    """``role`` is ``"agent"`` in the database — the ``"assistant"`` spelling
    only exists in the model-facing conversion. Testing for one of them left
    every real row unlabelled, so the judge could not tell an exchange the agent
    was already in from two other people talking."""
    rows = [
        {"role": "user", "content": "Alexa (user): status?"},
        {"role": "agent", "content": "all green",
         "metadata": {"channel_group": {"kind": "sent"}}},
        {"role": "assistant", "content": "and the queue is clear",
         "metadata": {"channel_group": {"kind": "sent"}}},
        {"role": "agent", "content": "[silent]",
         "metadata": {"channel_group": {"kind": "silent"}}},
        {"role": "user", "content": ""},
    ]
    assert render_recent_for_judge(rows, agent_name="Rex") == [
        "Alexa (user): status?",
        "Rex (you): all green",
        "Rex (you): and the queue is clear",
    ]


def test_the_agents_turns_are_labelled_with_its_account_name_when_it_has_one():
    """So the judge can match them against a message that addresses the agent by
    the name the group sees."""
    rows = [{"role": "agent", "content": "all green", "metadata": {}}]
    assert render_recent_for_judge(
        rows, agent_name="Rex", account_name="Rex Nguyen",
    ) == ["Rex Nguyen (you): all green"]


# ── helpers ───────────────────────────────────────────────────────────────


class _Exploding(_FakeLLM):
    async def chat_completion(self, **_kw):
        raise RuntimeError("provider is down")
        yield  # pragma: no cover — makes this an async generator


class _Hanging(_FakeLLM):
    async def chat_completion(self, **_kw):
        await asyncio.sleep(30)
        yield  # pragma: no cover


def _wire_llm(monkeypatch, llm):
    class _Agent:
        def low_performance_llm(self, _profile):
            return llm

    import app.events.runner as runner
    monkeypatch.setattr(runner, "get_cremind_agent", lambda: _Agent())


async def _judge(**overrides):
    kwargs = {
        "profile": "admin",
        "agent_name": "Rex",
        "agent_handle": "@opsbot",
        "account_name": "Rex Nguyen",
        "platform_name": "Telegram",
        "group_title": "Ops room",
        "members": ["Alexa Nguyen"],
        "recent": [],
        "message": "anyone seen the deploy?",
    }
    kwargs.update(overrides)
    return await judge_relevance(**kwargs)
