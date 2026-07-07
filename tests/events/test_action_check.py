"""Tests for the registration self-containment gate (``app.events.action_check``).

The gate runs ONE cheap-model classification to decide whether a registered
``action`` is self-contained (executable later with no other context). Pinned
behavior:

- a ``report_action_check`` tool call drives the decision;
- token usage from the DONE chunk is captured for cost attribution;
- it FAILS OPEN — no tool call, an unparseable value, or an odd-typed
  ``self_contained`` all resolve to ``self_contained=True`` so a valid
  registration is never blocked;
- ``gate_registration_action`` fail-opens when no server agent is wired (tests /
  slim CLI) or the LLM raises, and returns the failing result on a real reject.
"""

from __future__ import annotations

import asyncio
import json

from app.constants import ChatCompletionTypeEnum
import app.events.action_check as ac
from app.events.action_check import (
    check_action_self_contained,
    gate_registration_action,
)


class _FakeLLM:
    """Minimal LLMProvider stand-in: an optional FUNCTION_CALLING chunk then DONE."""

    def __init__(self, *, function_calls=None, tokens=None, raises=False):
        self._function_calls = function_calls
        self._tokens = tokens or {}
        self._raises = raises
        self.provider_name = "fake"
        self.model_name = "fake-mini"

    async def chat_completion(self, *, messages, tools, tool_choice):
        if self._raises:
            raise RuntimeError("provider boom")
        if self._function_calls is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": self._function_calls},
            }
        done = {"type": ChatCompletionTypeEnum.DONE}
        done.update(self._tokens)
        yield done


def _call(arguments=None):
    return [{"name": "report_action_check", "arguments": arguments if arguments is not None else {}}]


def _check(llm, action="Open the provided URL and email the result.", request_context=""):
    return asyncio.run(
        check_action_self_contained(llm=llm, action=action, request_context=request_context)
    )


# ── check_action_self_contained ────────────────────────────────────────────

def test_self_contained_true_with_tokens():
    llm = _FakeLLM(
        function_calls=_call({"self_contained": True, "reason": "everything inlined"}),
        tokens={"input_tokens": 90, "output_tokens": 5},
    )
    res = _check(llm)
    assert res.self_contained is True
    assert res.missing == []
    assert res.tokens["input_tokens"] == 90


def test_reject_surfaces_missing_list():
    llm = _FakeLLM(function_calls=_call({
        "self_contained": False,
        "missing": ["'the provided URL' — inline the full https:// address"],
        "reason": "URL is referenced but not included",
    }))
    res = _check(llm)
    assert res.self_contained is False
    assert res.missing and "https://" in res.missing[0]


def test_arguments_as_json_string_parsed():
    llm = _FakeLLM(function_calls=_call(json.dumps({
        "self_contained": False, "missing": ["url"], "reason": "no url"
    })))
    res = _check(llm)
    assert res.self_contained is False
    assert res.missing == ["url"]


def test_self_contained_string_coerced():
    llm = _FakeLLM(function_calls=_call({"self_contained": "false", "missing": ["x"], "reason": "s"}))
    assert _check(llm).self_contained is False


def test_no_tool_call_fails_open():
    llm = _FakeLLM(function_calls=None, tokens={"input_tokens": 40, "output_tokens": 2})
    res = _check(llm)
    assert res.self_contained is True
    assert res.tokens["input_tokens"] == 40  # tokens still captured


def test_unparseable_value_fails_open():
    # Missing / junk `self_contained` must not block a registration.
    for junk in ({"reason": "no field"}, {"self_contained": "maybe", "reason": "j"}):
        assert _check(_FakeLLM(function_calls=_call(junk))).self_contained is True
    # Explicit false-like strings still reject.
    for falsey in ("false", "0", "no", "off"):
        llm = _FakeLLM(function_calls=_call({"self_contained": falsey, "reason": "no"}))
        assert _check(llm).self_contained is False, f"{falsey!r} should reject"


def test_missing_cleared_when_self_contained():
    # A stray missing list on a self_contained=True result is dropped.
    llm = _FakeLLM(function_calls=_call({
        "self_contained": True, "missing": ["ignored"], "reason": "ok"
    }))
    assert _check(llm).missing == []


# ── gate_registration_action (orchestration) ───────────────────────────────

def _gate(**kw):
    return asyncio.run(gate_registration_action(**kw))


def test_gate_no_agent_fails_open(monkeypatch):
    # No server globals wired → the gate must not block (returns None).
    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: None)
    res = _gate(profile="p", action="do X", tool_name="schedule_create")
    assert res is None


def test_gate_llm_raises_fails_open(monkeypatch):
    class _Agent:
        def low_performance_llm(self, profile):
            return _FakeLLM(raises=True)

    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: _Agent())
    monkeypatch.setattr(ac, "record_action_check_usage", _noop_usage)
    res = _gate(profile="p", action="open the provided URL", tool_name="schedule_create")
    assert res is None  # provider error → fail open


def test_gate_empty_action_skips(monkeypatch):
    # Empty action is validated by each tool; the gate has nothing to judge.
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("should not resolve agent for empty action")

    monkeypatch.setattr("app.events.runner.get_cremind_agent", _boom)
    assert _gate(profile="p", action="   ", tool_name="schedule_create") is None
    assert called["n"] == 0


def test_gate_rejection_returns_result(monkeypatch):
    class _Agent:
        def low_performance_llm(self, profile):
            return _FakeLLM(function_calls=_call({
                "self_contained": False, "missing": ["the URL"], "reason": "url missing"
            }))

    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: _Agent())
    usage_calls = {"n": 0}

    async def _rec(**kw):
        usage_calls["n"] += 1

    monkeypatch.setattr(ac, "record_action_check_usage", _rec)
    res = _gate(profile="p", action="open the provided URL", tool_name="schedule_create",
                conversation_id="c1")
    assert res is not None and res.self_contained is False
    assert res.missing == ["the URL"]
    assert usage_calls["n"] == 1  # usage recorded best-effort


def test_gate_pass_returns_none(monkeypatch):
    class _Agent:
        def low_performance_llm(self, profile):
            return _FakeLLM(function_calls=_call({"self_contained": True, "reason": "ok"}))

    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: _Agent())
    monkeypatch.setattr(ac, "record_action_check_usage", _noop_usage)
    assert _gate(profile="p", action="email li@olli-ai.com the daily summary",
                 tool_name="schedule_create") is None


async def _noop_usage(**kw):
    return None
