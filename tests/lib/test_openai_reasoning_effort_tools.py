"""Some models reject an explicit ``reasoning_effort`` when function tools are
attached, on Chat Completions specifically:

    Function tools with reasoning_effort are not supported for gpt-5.4-mini in
    /v1/chat/completions. To use function tools, use /v1/responses or set
    reasoning_effort to 'none'.

That killed every agent turn on gpt-5.4-mini, since the reasoning loop always
attaches tools. Probing the live API showed *omitting* the parameter is accepted
(as is ``'none'``), so the provider drops it for tool-bearing requests rather
than sending ``'none'`` — omitting leaves the model reasoning at its own
default, whereas ``'none'`` would switch reasoning off entirely.

Detection is adaptive, with no model-name list: the restriction is per-model and
per-endpoint, so it's learned from one rejected request and memoized.

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

_TOOLS = [{
    "type": "function",
    "function": {"name": "get_time", "description": "Get the time", "parameters": {}},
}]


class _Captured(Exception):
    pass


class _ToolsEffortConflict(Exception):
    """Mirrors the real 400 text from the OpenAI SDK."""

    def __init__(self) -> None:
        super().__init__(
            "Error code: 400 - {'error': {'message': 'Function tools with "
            "reasoning_effort are not supported for gpt-5.4-mini in "
            "/v1/chat/completions. To use function tools, use /v1/responses or "
            "set reasoning_effort to \\'none\\'.', 'type': "
            "'invalid_request_error', 'param': 'reasoning_effort', 'code': None}}"
        )


def _make_provider(model="gpt-5.4-mini", *, effort="high", base_url=None, fail_times=0):
    calls: list[dict] = []
    state = {"remaining": fail_times}

    class _Completions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if state["remaining"] > 0:
                state["remaining"] -= 1
                raise _ToolsEffortConflict()
            raise _Captured()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    p = OpenAILLMProvider.__new__(OpenAILLMProvider)
    p.model_name = model
    p.base_url = base_url
    p._max_tokens_endpoint = base_url or "openai"
    p._sniff_max_completion_tokens = base_url is None
    p.default_reasoning_effort = effort
    p.encoder = None
    p.openai = _Client()
    return p, calls


async def _drain(agen):
    try:
        async for _ in agen:
            pass
    except (_Captured, Exception):
        pass


def _run(provider, **kwargs):
    kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
    asyncio.run(_drain(provider.chat_completion_stream(**kwargs)))


@pytest.fixture(autouse=True)
def _clear_memo():
    base._tools_reasoning_conflict_seen.clear()
    yield
    base._tools_reasoning_conflict_seen.clear()


# --- detection ------------------------------------------------------------


def test_detects_the_conflict_error():
    assert base.is_tools_reasoning_effort_conflict(_ToolsEffortConflict())


def test_does_not_confuse_it_with_other_400s():
    assert not base.is_tools_reasoning_effort_conflict(
        "Unsupported parameter: 'max_tokens' is not supported with this model."
    )
    assert not base.is_tools_reasoning_effort_conflict("rate limit exceeded")
    assert not base.is_tools_reasoning_effort_conflict("")


# --- adaptive retry -------------------------------------------------------


def test_conflict_triggers_retry_without_reasoning_effort():
    p, calls = _make_provider(fail_times=1)
    _run(p, tools=_TOOLS)

    assert len(calls) == 2, "should retry once after the conflict 400"
    assert calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in calls[1], "retry must omit the parameter"
    # Dropping the effort must not drop the tools — they're the whole point.
    assert calls[1]["tools"] == _TOOLS
    assert calls[1]["tool_choice"] == "auto"


def test_omits_rather_than_sending_none():
    """'none' would switch reasoning off; omitting leaves the model at its own
    default, which is why the fix omits."""
    p, calls = _make_provider(fail_times=1)
    _run(p, tools=_TOOLS)
    assert calls[1].get("reasoning_effort") is None
    assert "reasoning_effort" not in calls[1]


def test_retry_happens_without_a_retry_budget():
    p, calls = _make_provider(fail_times=1)
    _run(p, tools=_TOOLS, retry=None)
    assert len(calls) == 2


def test_memo_is_shared_across_provider_instances():
    """A fresh provider is built per agent turn, so the memo must be
    process-wide or every turn re-pays the 400."""
    first, _ = _make_provider(fail_times=1)
    _run(first, tools=_TOOLS)

    second, calls = _make_provider()
    _run(second, tools=_TOOLS)

    assert len(calls) == 1, "second instance must not repeat the failed request"
    assert "reasoning_effort" not in calls[0]


def test_effort_is_still_sent_when_no_tools_are_attached():
    """The restriction is tools-specific — a toolless call keeps the user's
    configured effort."""
    first, _ = _make_provider(fail_times=1)
    _run(first, tools=_TOOLS)  # teach the memo

    p, calls = _make_provider()
    _run(p)  # no tools
    assert calls[0]["reasoning_effort"] == "high"


def test_memo_is_scoped_to_the_endpoint():
    first, _ = _make_provider(base_url="https://gw-a.test/v1", fail_times=1)
    _run(first, tools=_TOOLS)

    other, calls = _make_provider(base_url="https://gw-b.test/v1")
    _run(other, tools=_TOOLS)
    assert calls[0]["reasoning_effort"] == "high"


def test_unrelated_models_are_unaffected():
    first, _ = _make_provider(model="gpt-5.4-mini", fail_times=1)
    _run(first, tools=_TOOLS)

    other, calls = _make_provider(model="gpt-4.1")
    _run(other, tools=_TOOLS)
    assert calls[0]["reasoning_effort"] == "high"


def test_retry_is_attempted_only_once():
    from app.lib.exception import AgentException

    p, calls = _make_provider(fail_times=99)

    async def _go():
        async for _ in p.chat_completion_stream(
            messages=[{"role": "user", "content": "hi"}], tools=_TOOLS
        ):
            pass

    with pytest.raises(AgentException):
        asyncio.run(_go())
    assert len(calls) == 2


def test_instant_mode_suppression_still_works():
    """The agent passes reasoning_effort='' in instant mode to suppress the
    param; that must keep working alongside the tools drop."""
    p, calls = _make_provider()
    _run(p, tools=_TOOLS, reasoning_effort="")
    assert "reasoning_effort" not in calls[0]


# --- non-streaming path ---------------------------------------------------


def test_non_streaming_path_adapts_too():
    p, calls = _make_provider(fail_times=1)

    async def _go():
        try:
            async for _ in p.chat_completion(
                messages=[{"role": "user", "content": "hi"}], tools=_TOOLS
            ):
                pass
        except Exception:
            pass

    asyncio.run(_go())
    assert len(calls) == 2
    assert "reasoning_effort" not in calls[1]
