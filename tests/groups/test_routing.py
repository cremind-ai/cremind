"""Tests for the group-chat routing classifier (``app.groups.routing``).

Routing spends one cheap-model call to name the agents worth waking, so the
room does not run N full turns to produce one answer. It is a HINT: the pinned
behavior below is mostly about the directions it is allowed to be wrong in.

- a ``route`` tool call drives the decision, and the sender is never a target;
- display names and unknown ids are resolved or dropped, never guessed at;
- it FAILS OPEN — disabled, no tool call, unparseable arguments, an empty or
  all-unknown target set, an exception and a timeout all resolve to
  ``everyone=True``, because an agent left out of the fan-out cannot answer at
  all while an extra one declines by itself.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.usage import UsageRecord
from app.constants import ChatCompletionTypeEnum
from app.groups import routing
from app.groups.routing import (
    ROUTING_SETTING_KEY,
    RoutingDecision,
    route_message,
    routing_usage_record,
    should_start_turn,
)

_MEMBERS = ["bird", "cat", "dog"]

# 'clone' deliberately answers to Rex as well — see the ambiguous-name test.
_AGENT_NAMES = {"bird": "Tweety", "cat": "Mimi", "dog": "Rex", "clone": "Rex"}
_PERSONAS = {
    "bird": "# Role\n\nI watch the calendar and never miss a meeting.\n",
    "cat": "I keep the household accounts.\n",
    "dog": "I run the errands.\n",
}


@pytest.fixture(autouse=True)
def _isolated_roster(monkeypatch):
    """Keep the roster off disk.

    ``read_persona_file`` CREATES the profile's PERSONA.md when it is missing, so
    an unpatched test would write into the developer's real ``~/.cremind``.
    """
    monkeypatch.setattr(routing, "read_agent_name", lambda p: _AGENT_NAMES.get(p, p))
    monkeypatch.setattr(routing, "read_persona_file", lambda p: _PERSONAS.get(p, ""))


class _FakeLLM:
    """Minimal stand-in for an LLMProvider.

    ``chat_completion`` yields an optional FUNCTION_CALLING chunk followed by a
    DONE chunk carrying token counts — the same shape the real providers emit.
    """

    def __init__(self, *, function_calls=None, tokens=None, raises=None, delay=0.0):
        self._function_calls = function_calls
        self._tokens = tokens or {}
        self._raises = raises
        self._delay = delay
        self.provider_name = "fake"
        self.model_name = "fake-mini"
        self.seen_messages = None

    async def chat_completion(self, *, messages, tools, tool_choice):
        self.seen_messages = messages
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._function_calls is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": self._function_calls},
            }
        done = {"type": ChatCompletionTypeEnum.DONE}
        done.update(self._tokens)
        yield done


def _call(arguments=None, name="route"):
    return [{"name": name, "arguments": arguments if arguments is not None else {}}]


def _group(members=None):
    return {"id": "g1", "name": "Household", "members": list(members or _MEMBERS)}


def _row(content="anyone free?", *, sender_profile=None, sender_kind="user",
         sender_name="Alexa", row_id="m9"):
    return {
        "id": row_id,
        "content": content,
        "sender_kind": sender_kind,
        "sender_name": sender_name,
        "sender_profile": sender_profile,
    }


# Distinct from ``None``, which is itself a settings value worth testing.
_ENABLED = object()


def _run(
    llm, *, row=None, members=None, settings=_ENABLED, recent_rows=(),
    nobody_eligible=False,
):
    return asyncio.run(
        route_message(
            group=_group(members),
            settings={ROUTING_SETTING_KEY: True} if settings is _ENABLED else settings,
            row=row if row is not None else _row(),
            recent_rows=recent_rows,
            llm=llm,
            nobody_eligible=nobody_eligible,
        )
    )


# ── the happy path ────────────────────────────────────────────────────────────


def test_targets_parsed_with_tokens_captured():
    llm = _FakeLLM(
        function_calls=_call({
            "targets": ["dog", "cat"], "everyone": False, "reason": "asked Rex and Mimi",
        }),
        tokens={"input_tokens": 310, "output_tokens": 12},
    )
    decision = _run(llm, row=_row("Rex, Mimi — can you two sort this out?"))

    assert decision.everyone is False
    assert decision.targets == {"dog", "cat"}
    assert decision.reason == "asked Rex and Mimi"
    assert decision.tokens["input_tokens"] == 310
    assert decision.tokens["output_tokens"] == 12
    assert decision.errored is False
    assert decision.model == "fake-mini"


def test_targets_resolved_by_agent_name():
    # The prompt asks for profile ids; models hand back the display name anyway,
    # and resolving those is what stops a needless fan-out. Case is not part of
    # the spelling — "Rex" and "mimi" both have to land.
    llm = _FakeLLM(function_calls=_call({
        "targets": ["Rex", "mimi"], "everyone": False, "reason": "by name",
    }))
    assert _run(llm).targets == {"dog", "cat"}


def test_a_name_two_members_answer_to_wakes_both_of_them():
    # An alias that means two things means both. Picking whichever was listed
    # first would silence the other, and silence is the one failure this
    # classifier must never produce — an agent woken by mistake declines itself.
    llm = _FakeLLM(function_calls=_call({
        "targets": ["Rex"], "everyone": False, "reason": "by name",
    }))
    decision = asyncio.run(
        route_message(
            group=_group(["dog", "clone"]),
            settings={ROUTING_SETTING_KEY: True},
            row=_row(),
            recent_rows=(),
            llm=llm,
        )
    )
    assert decision.everyone is False
    assert decision.targets == {"dog", "clone"}  # both answer to Rex


def test_a_display_name_colliding_with_another_members_id_wakes_both(
    monkeypatch,
):
    """``cat`` has named its agent "Dog" while a profile ``dog`` is in the room.

    Neither spelling can be resolved to one member without silencing the other,
    and there is no tie-break worth having: preferring the id loses ``cat``,
    preferring the name loses ``dog``. Both are woken and each decides for
    itself, which costs one turn and is the direction the whole classifier
    leans.
    """
    names = {"dog": "Rex", "cat": "Dog", "bird": "Tweety"}
    monkeypatch.setattr(routing, "read_agent_name", lambda p: names.get(p, p))

    # The id spelling still reaches the member that owns the id...
    llm = _FakeLLM(function_calls=_call({
        "targets": ["dog", "bird"], "everyone": False, "reason": "named both",
    }))
    decision = _run(llm)
    assert decision.everyone is False
    assert decision.targets == {"dog", "cat", "bird"}

    # ...and so does the name spelling, since they lower-case to one key.
    llm = _FakeLLM(function_calls=_call({
        "targets": ["Dog"], "everyone": False, "reason": "by name",
    }))
    assert _run(llm).targets == {"dog", "cat"}


def test_everyone_true_honoured():
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": True, "reason": "aimed at the room",
    }))
    decision = _run(llm)
    assert decision.everyone is True
    assert decision.targets == set()
    assert decision.reason == "aimed at the room"


def test_arguments_as_json_string_parsed():
    # Some providers serialize tool-call arguments as a JSON string.
    llm = _FakeLLM(function_calls=_call(json.dumps({
        "targets": ["cat"], "everyone": False, "reason": "accounts question",
    })))
    decision = _run(llm)
    assert decision.everyone is False
    assert decision.targets == {"cat"}


def test_targets_as_comma_string_parsed():
    llm = _FakeLLM(function_calls=_call({
        "targets": "dog, cat", "everyone": False, "reason": "both",
    }))
    assert _run(llm).targets == {"dog", "cat"}


# ── the sender never answers itself ───────────────────────────────────────────


def test_sender_is_never_a_target():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["dog", "cat"], "everyone": False, "reason": "picked the poster too",
    }))
    decision = _run(llm, row=_row(
        "I'll take this one — Mimi, can you confirm?",
        sender_profile="dog", sender_kind="agent", sender_name="Rex",
    ))
    assert decision.targets == {"cat"}
    assert "dog" not in decision.targets


def test_sender_is_absent_from_the_roster():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": False, "reason": "ok",
    }))
    _run(llm, row=_row(sender_profile="dog", sender_kind="agent", sender_name="Rex"))
    roster = llm.seen_messages[1]["content"]
    assert "profile_id=cat" in roster
    assert "profile_id=bird" in roster
    assert "profile_id=dog" not in roster


# ── every uncertain path wakes everyone ───────────────────────────────────────


def test_unknown_profile_ids_are_dropped():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat", "ghost", "admin"], "everyone": False, "reason": "one real",
    }))
    assert _run(llm).targets == {"cat"}


def test_only_unknown_profile_ids_falls_open():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["ghost", "nobody"], "everyone": False, "reason": "invented",
    }))
    decision = _run(llm)
    assert decision.everyone is True
    assert decision.targets == set()
    assert decision.errored is False


def test_empty_targets_falls_open():
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "reason": "nobody in particular",
    }))
    decision = _run(llm)
    assert decision.everyone is True
    assert decision.targets == set()


def test_no_tool_call_falls_open_but_still_bills():
    llm = _FakeLLM(function_calls=None, tokens={"input_tokens": 240, "output_tokens": 4})
    decision = _run(llm)
    assert decision.everyone is True
    assert decision.errored is False
    # Tokens are captured even when the decision itself is unusable.
    assert decision.tokens["input_tokens"] == 240


def test_unparseable_arguments_fall_open():
    for arguments in ("not json at all", {"reason": "no fields"}, []):
        decision = _run(_FakeLLM(function_calls=_call(arguments)))
        assert decision.everyone is True, f"{arguments!r} should fail open"
        assert decision.targets == set()


def test_junk_everyone_value_leaves_the_decision_to_targets():
    # An out-of-schema 'everyone' must not read as False and narrow the room on
    # the strength of a junk string; targets still decide, and still fail open.
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": "maybe", "reason": "junk flag",
    }))
    assert _run(llm).targets == {"cat"}

    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": "maybe", "reason": "junk flag",
    }))
    assert _run(llm).everyone is True


def test_raising_llm_falls_open_and_marks_errored():
    llm = _FakeLLM(raises=RuntimeError("provider exploded"))
    decision = _run(llm)
    assert decision.everyone is True
    assert decision.targets == set()
    assert decision.errored is True


def test_timeout_falls_open():
    monkey = routing._ROUTING_TIMEOUT_S
    routing._ROUTING_TIMEOUT_S = 0.01
    try:
        llm = _FakeLLM(
            function_calls=_call({"targets": ["cat"], "everyone": False, "reason": "x"}),
            delay=5.0,
        )
        decision = _run(llm)
    finally:
        routing._ROUTING_TIMEOUT_S = monkey
    assert decision.everyone is True
    assert decision.targets == set()
    assert decision.errored is True


def test_missing_llm_falls_open():
    decision = _run(None)
    assert decision.everyone is True
    assert decision.errored is True


def test_disabled_never_calls_the_model():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": False, "reason": "should not be asked",
    }))
    decision = _run(llm, settings={ROUTING_SETTING_KEY: False})
    assert decision.everyone is True
    assert decision.errored is False
    assert llm.seen_messages is None


def test_an_absent_setting_reads_as_the_default_rather_than_off():
    """There is ONE definition of the knob, in ``app.groups.settings``.

    It briefly existed twice with opposite readings of a missing key — off here,
    on there — which no call path could expose, because ``post_message``
    normalizes the blob before it routes and the key is always present by then.
    A room stored before the knob existed did not decline routing, so an absent
    key takes the default like the numeric caps do.
    """
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": False, "reason": "by name",
    }))
    for settings in ({}, None):
        assert _run(llm, settings=settings).targets == {"cat"}


def test_single_candidate_skips_the_call():
    # One possible answerer and no "nobody" on the table: the only answers are
    # "wake it" and "wake it", so the classification would cost more than the
    # turn it could save.
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": True, "reason": "should not be asked",
    }))
    decision = _run(llm, members=["dog", "cat"],
                    row=_row(sender_profile="dog", sender_kind="agent"))
    assert decision.everyone is True
    assert llm.seen_messages is None


# ── nobody: an agent's own reply that asks nothing of anyone ─────────────────


def test_a_reply_that_asks_nothing_wakes_nobody():
    """The bug this exists for: Rex answers the person, and Mimi spends a whole
    turn reading it only to say ``[silent]``."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True,
        "reason": "Rex answered Alexa; nothing asked of the others",
    }))
    decision = _run(
        llm,
        row=_row("It is 14:20.", sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is True
    # Set WITH everyone=False, so a consumer reading either field first agrees.
    assert decision.everyone is False
    assert decision.targets == set()
    assert should_start_turn(decision, "cat") is False


def test_one_candidate_is_worth_classifying_when_nobody_is_possible():
    """The two-member room from the report: one candidate, but the second
    possible answer is "no turn at all"."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "a report",
    }))
    decision = _run(
        llm, members=["dog", "cat"],
        row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert llm.seen_messages is not None
    assert decision.nobody is True


def test_no_candidates_skips_even_a_reply():
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "nope",
    }))
    decision = _run(
        llm, members=["dog"],
        row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert llm.seen_messages is None
    assert decision.nobody is False


def test_nobody_is_refused_for_a_persons_message():
    """A person always deserves an answer. Even asked for, even eligible: the
    sender kind decides, because a room that silently ignores a human is the one
    failure routing must never produce."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "confused",
    }))
    decision = _run(llm, row=_row("hello?"), nobody_eligible=True)
    assert decision.nobody is False
    assert decision.everyone is True


def test_nobody_is_refused_for_a_tool_post():
    """``send_group_message`` and ``as_profile`` posts are agent rows too, but
    they are somebody addressing the room on purpose — the caller does not mark
    them eligible, and an unasked-for ``nobody`` changes nothing."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "a report",
    }))
    decision = _run(
        llm, row=_row("status?", sender_profile="dog", sender_kind="agent"),
    )
    assert decision.nobody is False
    assert decision.everyone is True


def test_named_targets_beat_a_nobody_claim():
    """A contradiction resolves the forgiving way: the named agent is woken."""
    llm = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": False, "nobody": True, "reason": "both",
    }))
    decision = _run(
        llm, row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is False
    assert decision.targets == {"cat"}


def test_everyone_beats_a_nobody_claim():
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": True, "nobody": True, "reason": "both",
    }))
    decision = _run(
        llm, row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is False
    assert decision.everyone is True


def test_a_junk_nobody_value_falls_open():
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": "maybe", "reason": "unsure",
    }))
    decision = _run(
        llm, row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is False
    assert decision.everyone is True


def test_a_reply_gets_a_tighter_deadline(monkeypatch):
    """Its classification runs inside the turn's finalization — the seat's
    ``complete`` frame and idle status wait behind it — so a hung provider must
    give up sooner than it may on a person's post.

    Driven through the real call rather than compared as constants: the two
    numbers can sit in the file in the right order while the code passes the
    wrong one."""
    monkeypatch.setattr(routing, "_ROUTING_TIMEOUT_AGENT_S", 0.01)
    monkeypatch.setattr(routing, "_ROUTING_TIMEOUT_S", 30.0)
    answer = _call({"targets": [], "everyone": False, "nobody": True, "reason": "r"})

    slow = _FakeLLM(function_calls=answer, delay=0.2)
    decision = _run(
        slow, row=_row(sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.errored is True   # gave up at the agent budget...
    assert decision.everyone is True  # ...and fell open, as every failure does

    # The same delay is nowhere near the person's budget.
    patient = _FakeLLM(function_calls=_call({
        "targets": ["cat"], "everyone": False, "reason": "by name",
    }), delay=0.2)
    assert _run(patient).targets == {"cat"}


def test_the_candidate_rule_is_spelled_once():
    """``fanout`` applies it before resolving a model, so the two must agree."""
    assert routing.min_candidates(True) == 1
    assert routing.min_candidates(False) == 2


# ── the prompt ────────────────────────────────────────────────────────────────


def test_prompt_carries_roster_recent_rows_and_the_new_message():
    llm = _FakeLLM(function_calls=_call({
        "targets": ["bird"], "everyone": False, "reason": "calendar",
    }))
    recent = [
        _row("morning all", row_id="m7"),
        _row("Ready.", sender_profile="cat", sender_kind="agent",
             sender_name="Mimi", row_id="m8"),
        # The row being routed usually sits at the end of the slice the caller
        # read back; it must not also appear in the history.
        _row("what's on today?", row_id="m9"),
    ]
    _run(llm, row=_row("what's on today?", row_id="m9"), recent_rows=recent)

    system, user = llm.seen_messages
    assert system["content"] is routing._ROUTING_SYSTEM_PROMPT  # cache-friendly constant
    body = user["content"]
    assert "Household" in body
    assert "profile_id=bird | name=Tweety | role:" in body
    assert "I watch the calendar" in body  # persona, headings stripped
    assert "# Role" not in body
    assert "Alexa (user): morning all" in body
    assert "Mimi (agent): Ready." in body
    assert body.count("what's on today?") == 1


def test_the_prompt_and_schema_teach_the_nobody_outcome():
    """Both halves, because the model needs the rule and the field to report it
    — and the field must not be offered without the rule that a person's
    message never qualifies."""
    prompt = routing._ROUTING_SYSTEM_PROMPT
    assert "nobody=true" in prompt
    assert "nobody is only ever right for an assistant's post" in prompt
    schema = routing._build_routing_tools()[0]["function"]["parameters"]
    assert "nobody" in schema["properties"]
    assert "nobody" in schema["required"]


# ── applying the hint ─────────────────────────────────────────────────────────


def test_should_start_turn():
    narrowed = RoutingDecision(targets={"cat"}, everyone=False)
    assert should_start_turn(narrowed, "cat") is True
    assert should_start_turn(narrowed, "dog") is False
    # Fail-open decisions wake every member.
    assert should_start_turn(RoutingDecision(), "dog") is True
    # Nobody wakes nobody — and wins over a contradictory stamp, so a malformed
    # row cannot wake the room it was meant to leave alone.
    assert should_start_turn(RoutingDecision(nobody=True, everyone=False), "dog") is False
    assert should_start_turn(RoutingDecision(nobody=True), "dog") is False
    assert should_start_turn(
        RoutingDecision(targets={"dog"}, everyone=False, nobody=True), "dog",
    ) is False


# ── cost attribution ──────────────────────────────────────────────────────────


def test_usage_record_shape():
    llm = _FakeLLM(
        function_calls=_call({"targets": ["cat"], "everyone": False, "reason": "x"}),
        tokens={
            "input_tokens": 300,
            "output_tokens": 11,
            "cache_read_input_tokens": 64,
            "cache_creation_input_tokens": 5,
        },
    )
    decision = _run(llm)
    record = routing_usage_record(decision, llm, group_name="Household")

    assert isinstance(record, UsageRecord)
    assert record.source_kind == "group_routing"
    assert record.label == "Routing: Household"
    assert record.tool_id is None
    assert record.provider == "fake"
    assert record.model == "fake-mini"
    assert record.model_group is None
    assert record.step_index == 0
    assert record.input_tokens == 300
    assert record.output_tokens == 11
    assert record.cache_read_input_tokens == 64
    assert record.cache_creation_input_tokens == 5
    assert record.to_dict()["source_kind"] == "group_routing"


def test_usage_record_skipped_when_nothing_was_spent():
    # Routing that never called a model (disabled, single candidate) has no cost
    # to attribute, so the caller can persist unconditionally.
    assert routing_usage_record(RoutingDecision(), _FakeLLM()) is None
    assert routing_usage_record(
        RoutingDecision(tokens={"input_tokens": 10}), None,
    ) is None


# ── the last guard on the one answer that fails closed ──────────────────────


@pytest.mark.parametrize("content", [
    "Mimi, can you check the budget?",
    "could mimi take this one",  # the spelling is matched case-insensitively
    "handing this to cat",
    "Tweety knows the calendar",
])
def test_a_reply_that_names_a_member_is_never_routed_to_nobody(content):
    """Everywhere else an uncertain classification wakes everybody and costs a
    turn. ``nobody`` costs the room an answer nobody will ask for again, so a
    model that calls "Mimi, can you check the budget?" a closing remark is
    overruled by the text itself."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "a report",
    }))
    decision = _run(
        llm, row=_row(content, sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is False
    assert decision.everyone is True


def test_a_reply_naming_nobody_still_routes_to_nobody():
    """The guard must not swallow the outcome it protects."""
    llm = _FakeLLM(function_calls=_call({
        "targets": [], "everyone": False, "nobody": True, "reason": "a report",
    }))
    decision = _run(
        llm,
        row=_row("It is 14:20 here.", sender_profile="dog", sender_kind="agent"),
        nobody_eligible=True,
    )
    assert decision.nobody is True


# ── reading a stamp back ────────────────────────────────────────────────────


def test_a_stamp_round_trips():
    decision = routing.decision_from_stamp({
        "targets": ["cat", "bird"], "everyone": False, "nobody": False,
        "reason": "by name", "errored": False, "model": "fake-mini",
    })
    assert decision.targets == {"cat", "bird"}
    assert decision.everyone is False
    assert decision.nobody is False
    assert decision.model == "fake-mini"


@pytest.mark.parametrize("stamp", [
    None, "nonsense", 7, [],
    {"targets": "cat", "everyone": False},    # a bare string iterates per CHARACTER
    {"targets": {"cat": True}, "everyone": False},
])
def test_an_unreadable_stamp_reads_as_no_decision(stamp):
    """``None`` means "wake everybody", which is what every caller does with it.

    The string case is the one that bit: read leniently in one place and
    strictly in another, the same corrupt stamp made the sweep quiet-deliver to
    every member — losing the post its only answer — while the note beside it
    spelled the target letter by letter, ``[to: c, a, t]``."""
    assert routing.decision_from_stamp(stamp) is None


def test_a_stamp_from_before_the_outcome_reads_as_somebody():
    decision = routing.decision_from_stamp({"targets": ["cat"], "everyone": False})
    assert decision.nobody is False
    assert routing.should_start_turn(decision, "cat") is True


def test_an_absent_targets_key_is_empty_not_unreadable():
    decision = routing.decision_from_stamp({"everyone": True})
    assert decision is not None
    assert decision.targets == set()
    assert decision.everyone is True
