"""OpenAI's GPT-5 / o-series reject ``max_tokens`` on Chat Completions and
demand ``max_completion_tokens``. Two layers cover that:

  * against OpenAI itself the model family is recognized by name, so the very
    first request already uses the right key;
  * against anything else (gateways, proxies, local servers) nothing changes
    until the endpoint actually rejects a request — then the provider renames,
    retries, and memoizes the endpoint so later requests get it right too.

Params are captured by stubbing the client's completions.create, same approach
as ``test_groq_params.py``.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("openai")
pytest.importorskip("tiktoken")

from app.lib.llm import base  # noqa: E402
from app.lib.llm.openai import OpenAILLMProvider  # noqa: E402


class _Captured(Exception):
    pass


class _Unsupported(Exception):
    """Mirrors the real 400 text from the OpenAI SDK."""

    def __init__(self) -> None:
        super().__init__(
            "Error code: 400 - {'error': {'message': \"Unsupported parameter: "
            "'max_tokens' is not supported with this model. Use "
            "'max_completion_tokens' instead.\", 'type': 'invalid_request_error', "
            "'param': 'max_tokens', 'code': 'unsupported_parameter'}}"
        )


def _make_provider(model: str, *, base_url: str | None = None, fail_times: int = 0):
    """Build a provider whose create() records kwargs and never really calls out.

    ``fail_times`` create() calls raise the unsupported-parameter 400 first, so
    the adaptive path can be exercised.
    """
    calls: list[dict] = []
    state = {"remaining": fail_times}

    class _Completions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if state["remaining"] > 0:
                state["remaining"] -= 1
                raise _Unsupported()
            raise _Captured()  # params recorded; skip response parsing

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    # __new__ bypasses __init__ so we don't build a real client / tiktoken encoder.
    p = OpenAILLMProvider.__new__(OpenAILLMProvider)
    p.model_name = model
    p.base_url = base_url
    p._max_tokens_endpoint = base_url or "openai"
    p._sniff_max_completion_tokens = base_url is None
    p.default_reasoning_effort = None
    p.encoder = None
    p.openai = _Client()
    return p, calls


async def _drain(agen):
    try:
        async for _ in agen:
            pass
    except (_Captured, Exception):
        pass


def _run_stream(provider, **kwargs):
    asyncio.run(
        _drain(
            provider.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}], **kwargs
            )
        )
    )


@pytest.fixture(autouse=True)
def _clear_memo():
    """The memo is module-level by design; keep tests independent of each other."""
    base._max_completion_tokens_seen.clear()
    yield
    base._max_completion_tokens_seen.clear()


# --- name sniffing -------------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-5.4-mini", "gpt-5.4", "o3", "o4-mini", "o1"])
def test_openai_reasoning_models_send_max_completion_tokens(model):
    p, calls = _make_provider(model)
    _run_stream(p, max_tokens=32768)
    assert calls[0]["max_completion_tokens"] == 32768
    assert "max_tokens" not in calls[0]


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o", "gpt-oss-120b", "o-my-model"])
def test_other_openai_models_still_send_max_tokens(model):
    p, calls = _make_provider(model)
    _run_stream(p, max_tokens=2048)
    assert calls[0]["max_tokens"] == 2048
    assert "max_completion_tokens" not in calls[0]


def test_gateway_is_not_sniffed_by_name():
    """A proxied ``openai/gpt-5.4`` keeps ``max_tokens`` until the gateway
    complains — some OpenAI-compatible gateways only accept the old name."""
    p, calls = _make_provider("openai/gpt-5.4", base_url="https://openrouter.ai/api/v1")
    _run_stream(p, max_tokens=1000)
    assert calls[0]["max_tokens"] == 1000
    assert "max_completion_tokens" not in calls[0]


# --- adaptive retry + memo ----------------------------------------------


def test_unsupported_400_triggers_rename_and_retry():
    p, calls = _make_provider("some-proxy-model", base_url="https://gw.test/v1", fail_times=1)
    _run_stream(p, max_tokens=4096)

    assert len(calls) == 2, "should retry once after the unsupported-parameter 400"
    assert calls[0]["max_tokens"] == 4096
    assert calls[1]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[1]


def test_rename_retry_happens_without_a_retry_budget():
    """The caller passes retry=None by default; the rename must still retry,
    otherwise the memo would never be populated and every turn would fail."""
    p, calls = _make_provider("some-proxy-model", base_url="https://gw.test/v1", fail_times=1)
    _run_stream(p, max_tokens=4096, retry=None)
    assert len(calls) == 2


def test_memo_is_shared_across_provider_instances():
    """create_llm_provider does not cache, so a fresh provider is built per
    agent. The memo must be process-wide or the 400 is re-paid every turn."""
    first, _ = _make_provider("some-proxy-model", base_url="https://gw.test/v1", fail_times=1)
    _run_stream(first, max_tokens=4096)

    second, calls = _make_provider("some-proxy-model", base_url="https://gw.test/v1")
    _run_stream(second, max_tokens=4096)

    assert len(calls) == 1, "second instance must not repeat the failed request"
    assert calls[0]["max_completion_tokens"] == 4096


def test_memo_is_scoped_to_the_endpoint():
    """Same model id behind a different gateway must not inherit the verdict."""
    first, _ = _make_provider("shared-id", base_url="https://gw-a.test/v1", fail_times=1)
    _run_stream(first, max_tokens=100)

    other, calls = _make_provider("shared-id", base_url="https://gw-b.test/v1")
    _run_stream(other, max_tokens=100)

    assert calls[0]["max_tokens"] == 100


def test_rename_retry_is_attempted_only_once():
    """A stubborn endpoint that keeps 400ing must surface the error rather than
    loop forever."""
    from app.lib.exception import AgentException

    p, calls = _make_provider("stubborn", base_url="https://gw.test/v1", fail_times=99)

    async def _go():
        async for _ in p.chat_completion_stream(
            messages=[{"role": "user", "content": "hi"}], max_tokens=10
        ):
            pass

    with pytest.raises(AgentException):
        asyncio.run(_go())
    assert len(calls) == 2


# --- temperature=0 regression -------------------------------------------


def test_explicit_zero_temperature_is_sent():
    p, calls = _make_provider("gpt-4.1")
    _run_stream(p, temperature=0.0)
    assert calls[0]["temperature"] == 0.0


def test_absent_temperature_is_omitted():
    p, calls = _make_provider("gpt-4.1")
    _run_stream(p)
    assert "temperature" not in calls[0]
