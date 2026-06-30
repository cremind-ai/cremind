"""Tests for the skill-event matching gate (``app.events.gate``).

The gate runs ONE cheap-model classification to decide whether an event's
content satisfies a subscription's natural-language action. Pinned behavior:

- a ``report_match`` tool call drives the decision (true/false);
- token usage from the DONE chunk is captured for cost attribution;
- it FAILS OPEN — no tool call, an unparseable value, or odd-typed ``matches``
  all resolve to ``matched=True`` so a real event is never dropped.
"""

from __future__ import annotations

import asyncio
import json

from app.constants import ChatCompletionTypeEnum
from app.events.gate import classify_event_match


class _FakeLLM:
    """Minimal stand-in for an LLMProvider.

    ``chat_completion`` yields an optional FUNCTION_CALLING chunk followed by a
    DONE chunk carrying token counts — the same shape the real providers emit.
    """

    def __init__(self, *, function_calls=None, tokens=None):
        self._function_calls = function_calls
        self._tokens = tokens or {}
        self.provider_name = "fake"
        self.model_name = "fake-mini"

    async def chat_completion(self, *, messages, tools, tool_choice):
        if self._function_calls is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": self._function_calls},
            }
        done = {"type": ChatCompletionTypeEnum.DONE}
        done.update(self._tokens)
        yield done


def _call(call_name="report_match", arguments=None):
    return [{"name": call_name, "arguments": arguments if arguments is not None else {}}]


def _run(llm):
    return asyncio.run(
        classify_event_match(
            llm=llm,
            event_type="new_email",
            action="Notify me if the new email is from li@olli-ai.com",
            file_content="from: admin@cremind.io\nsubject: Hello",
        )
    )


def test_match_true_with_tokens_captured():
    llm = _FakeLLM(
        function_calls=_call(arguments={"matches": True, "reason": "from the right sender"}),
        tokens={"input_tokens": 120, "output_tokens": 8},
    )
    res = _run(llm)
    assert res.matched is True
    assert res.reason == "from the right sender"
    assert res.tokens["input_tokens"] == 120
    assert res.tokens["output_tokens"] == 8


def test_match_false():
    llm = _FakeLLM(
        function_calls=_call(arguments={"matches": False, "reason": "sender is admin@cremind.io, not li@olli-ai.com"}),
    )
    res = _run(llm)
    assert res.matched is False
    assert "admin@cremind.io" in res.reason


def test_arguments_as_json_string_parsed():
    # Some providers serialize tool-call arguments as a JSON string.
    llm = _FakeLLM(function_calls=_call(arguments=json.dumps({"matches": False, "reason": "no"})))
    res = _run(llm)
    assert res.matched is False


def test_matches_string_coerced():
    llm = _FakeLLM(function_calls=_call(arguments={"matches": "false", "reason": "stringy"}))
    res = _run(llm)
    assert res.matched is False


def test_no_tool_call_fails_open():
    # Model emitted no function call → never drop a real event.
    llm = _FakeLLM(function_calls=None, tokens={"input_tokens": 50, "output_tokens": 3})
    res = _run(llm)
    assert res.matched is True
    # Tokens are still captured even when the decision falls back to match.
    assert res.tokens["input_tokens"] == 50


def test_unparseable_matches_fails_open():
    llm = _FakeLLM(function_calls=_call(arguments={"reason": "missing matches field"}))
    res = _run(llm)
    assert res.matched is True


def test_junk_string_matches_fails_open():
    # An out-of-schema junk string for `matches` must NOT coerce to False and
    # drop the event — unrecognized values fail open (match), per the contract.
    for junk in ("", "maybe", "unknown", "n/a"):
        llm = _FakeLLM(function_calls=_call(arguments={"matches": junk, "reason": "junk"}))
        assert _run(llm).matched is True, f"junk value {junk!r} should fail open"
    # Explicit false-like strings still resolve to False.
    for falsey in ("false", "0", "no", "off"):
        llm = _FakeLLM(function_calls=_call(arguments={"matches": falsey, "reason": "no"}))
        assert _run(llm).matched is False, f"{falsey!r} should be False"
