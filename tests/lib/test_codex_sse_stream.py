"""Tests for the Codex SSE → Cremind yield-contract translation and retry/error
handling (app/lib/llm/openai_codex.py). ``_iter_events``/``_headers`` are stubbed
with canned events per attempt — no network, no auth, no real client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.constants import ChatCompletionTypeEnum
from app.constants.status import Status
from app.lib.exception import AgentException
from app.lib.llm import openai_codex as oc

_MSGS = [{"role": "user", "content": "hi"}]


def _make_provider(event_batches):
    """event_batches: list (one per attempt) of either a list of SSE event dicts
    or an Exception to raise on that attempt."""
    p = oc.CodexLLMProvider.__new__(oc.CodexLLMProvider)
    p.config_storage = None
    p.profile = None
    p.model_name = "gpt-5.6-sol"
    p.default_reasoning_effort = None
    p.session_id = "sess"
    p.encoder = SimpleNamespace(encode=lambda s: list(s or ""))
    p._client = None

    attempts = {"i": 0}

    async def _headers():
        return {}

    async def _iter(headers, body):
        batch = event_batches[min(attempts["i"], len(event_batches) - 1)]
        attempts["i"] += 1
        if isinstance(batch, Exception):
            raise batch
        for ev in batch:
            yield ev

    p._headers = _headers
    p._iter_events = _iter
    p._attempts = attempts
    return p


async def _collect(agen):
    out = []
    async for e in agen:
        out.append(e)
    return out


def _run(agen):
    return asyncio.run(_collect(agen))


# ── content + usage ─────────────────────────────────────────────────────────

def test_text_deltas_and_done():
    events = [
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    p = _make_provider([events])
    out = _run(p.chat_completion_stream(messages=_MSGS))
    contents = [e["data"] for e in out if e["type"] == ChatCompletionTypeEnum.CONTENT]
    assert contents == ["Hel", "lo"]
    done = next(e for e in out if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["data"] == "Hello" and done["finish_reason"] == "stop"


def test_function_call_chunk():
    events = [
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call_1", "name": "do_it", "arguments": '{"x":1}'}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    p = _make_provider([events])
    out = _run(p.chat_completion_stream(messages=_MSGS))
    fc = next(e for e in out if e["type"] == ChatCompletionTypeEnum.FUNCTION_CALLING)
    fn = fc["data"]["function"][0]
    assert fn["id"] == "call_1" and fn["name"] == "do_it" and fn["arguments"] == {"x": 1}
    assert isinstance(fc["data"]["outputToken"], int)
    done = next(e for e in out if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["finish_reason"] == "tool_calls"


def test_usage_cached_split():
    events = [{"type": "response.completed", "response": {"usage": {
        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 30}, "output_tokens": 20}}}]
    p = _make_provider([events])
    done = next(e for e in _run(p.chat_completion_stream(messages=_MSGS)) if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["input_tokens"] == 70
    assert done["cache_read_input_tokens"] == 30
    assert done["cache_creation_input_tokens"] == 0
    assert done["output_tokens"] == 20


def test_missing_usage_all_none():
    p = _make_provider([[{"type": "response.completed", "response": {}}]])
    done = next(e for e in _run(p.chat_completion_stream(messages=_MSGS)) if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["input_tokens"] is None and done["output_tokens"] is None


def test_incomplete_max_output_is_length():
    events = [{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}]
    p = _make_provider([events])
    done = next(e for e in _run(p.chat_completion_stream(messages=_MSGS)) if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["finish_reason"] == "length"


# ── retry / error classification ────────────────────────────────────────────

def test_transient_error_retried_then_success():
    err_batch = [{"type": "error", "error": {"message": "server_is_overloaded"}}]
    ok_batch = [
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    p = _make_provider([err_batch, ok_batch])
    out = _run(p.chat_completion_stream(messages=_MSGS, retry=1))
    done = next(e for e in out if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["data"] == "ok"
    assert p._attempts["i"] == 2  # retried once


def test_transient_error_exhausted_raises():
    err_batch = [{"type": "error", "error": {"message": "model_at_capacity"}}]
    p = _make_provider([err_batch])
    with pytest.raises(AgentException) as ei:
        _run(p.chat_completion_stream(messages=_MSGS, retry=0))
    assert ei.value.code == Status.LLM_CHAT_COMPLETION_ERROR


def test_context_overflow_no_retry():
    err_batch = [{"type": "error", "error": {"message": "context length exceeded — reduce the length"}}]
    p = _make_provider([err_batch, err_batch])
    with pytest.raises(AgentException) as ei:
        _run(p.chat_completion_stream(messages=_MSGS, retry=3))
    assert ei.value.code == Status.LLM_CONTEXT_OVERFLOW
    assert p._attempts["i"] == 1  # not retried


def test_raise_http_error_classification():
    p = _make_provider([[]])
    with pytest.raises(AgentException) as ei:
        p._raise_http_error(401, "unauthorized")
    assert ei.value.code == Status.LLM_CHAT_COMPLETION_ERROR
    with pytest.raises(AgentException):
        p._raise_http_error(429, '{"error":{"resets_in_seconds":30}}')
    with pytest.raises(oc.CodexTransientError):
        p._raise_http_error(503, "server_is_overloaded")
    with pytest.raises(RuntimeError):
        p._raise_http_error(400, "some other 4xx")
    # A "model not supported" 400 is turned into an actionable AgentException that
    # names the model — not a cryptic RuntimeError buried in a traceback.
    p.model_name = "gpt-4.1-mini"
    body = ('{"detail":"The \'gpt-4.1-mini\' model is not supported when using '
            'Codex with a ChatGPT account."}')
    with pytest.raises(AgentException) as ei2:
        p._raise_http_error(400, body)
    assert ei2.value.code == Status.LLM_CHAT_COMPLETION_ERROR
    assert "gpt-4.1-mini" in str(ei2.value)


# ── non-streaming collect ───────────────────────────────────────────────────

def test_chat_completion_json_schema_collect():
    events = [
        {"type": "response.output_text.delta", "delta": '{"a":1}'},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    p = _make_provider([events])
    rf = {"type": "json_schema", "json_schema": {"name": "O", "schema": {}}}
    out = _run(p.chat_completion(messages=_MSGS, response_format=rf))
    # json_schema text is delivered as a FUNCTION_CALLING("json_schema"), not CONTENT
    assert not any(e["type"] == ChatCompletionTypeEnum.CONTENT for e in out)
    fc = next(e for e in out if e["type"] == ChatCompletionTypeEnum.FUNCTION_CALLING)
    assert fc["data"]["function"][0]["name"] == "json_schema"
    assert fc["data"]["function"][0]["arguments"] == {"a": 1}


def test_chat_completion_plain_text():
    events = [
        {"type": "response.output_text.delta", "delta": "hello world"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    p = _make_provider([events])
    out = _run(p.chat_completion(messages=_MSGS))
    content = next(e for e in out if e["type"] == ChatCompletionTypeEnum.CONTENT)
    assert content["data"] == "hello world"
    done = next(e for e in out if e["type"] == ChatCompletionTypeEnum.DONE)
    assert done["data"] == "hello world"
