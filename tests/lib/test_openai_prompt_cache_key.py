"""Routing OpenAI's automatic prompt cache.

OpenAI caches a long prompt prefix on its own, but routes the lookup on an
opaque key; without one, requests that share a prefix are not guaranteed to
reach the same cache. Cremind was sending no key at all on the Chat Completions
path — ``args={"prompt_cache": True}`` was only ever read by the Anthropic
provider — and a channel-group conversation read **zero** cached tokens on an
eighteen-thousand-token prompt, turn after turn.

The other half of the fix lives in the reasoning agent (a key that is stable for
the conversation, rather than the Codex provider's per-instance ``uuid4``); this
file covers the wire.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("openai")
pytest.importorskip("tiktoken")

from app.lib.llm.openai import OpenAILLMProvider  # noqa: E402


class _Captured(Exception):
    pass


def _provider(*, base_url: str | None = None):
    calls: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            raise _Captured()  # params recorded; skip response parsing

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    # __new__ bypasses __init__ so no real client or tiktoken encoder is built.
    p = OpenAILLMProvider.__new__(OpenAILLMProvider)
    p.model_name = "gpt-5.4-mini"
    p.base_url = base_url
    p._max_tokens_endpoint = base_url or "openai"
    p._sniff_max_completion_tokens = base_url is None
    p.default_reasoning_effort = None
    p.encoder = None
    p.openai = _Client()
    return p, calls


def _run(provider, *, stream: bool = True, **kwargs):
    async def _drain():
        agen = (
            provider.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}], **kwargs,
            ) if stream else provider.chat_completion(
                messages=[{"role": "user", "content": "hi"}], **kwargs,
            )
        )
        try:
            async for _ in agen:
                pass
        except Exception:  # noqa: BLE001
            pass

    asyncio.run(_drain())


@pytest.mark.parametrize("stream", [True, False])
def test_the_key_is_sent_when_the_caller_gives_one(stream):
    provider, calls = _provider()
    _run(provider, stream=stream, args={
        "prompt_cache": True, "prompt_cache_key": "admin:conv-1",
    })
    assert calls[0]["prompt_cache_key"] == "admin:conv-1"


@pytest.mark.parametrize("stream", [True, False])
def test_nothing_is_sent_when_the_caller_gives_none(stream):
    """Including on the old ``{"prompt_cache": True}``-only shape, which several
    call sites still pass."""
    provider, calls = _provider()
    _run(provider, stream=stream, args={"prompt_cache": True})
    assert "prompt_cache_key" not in calls[0]

    provider, calls = _provider()
    _run(provider, stream=stream)
    assert "prompt_cache_key" not in calls[0]


def test_it_is_not_sent_to_an_openai_compatible_server():
    """Groq, Ollama, vLLM and gateways all speak this API without implementing
    every parameter, and several REJECT an unknown one rather than ignoring it —
    which would break every call to save a cache miss."""
    provider, calls = _provider(base_url="https://api.groq.com/openai/v1")
    _run(provider, args={"prompt_cache": True, "prompt_cache_key": "admin:conv-1"})
    assert "prompt_cache_key" not in calls[0]


# ── the key the agent chooses ─────────────────────────────────────────────


def test_the_agent_keys_the_cache_on_the_conversation():
    """Stable for the conversation and across restarts. The Codex provider's
    fallback is a fresh ``uuid4`` per instance, and a provider instance is built
    per turn — so without this every turn asked for a cache nobody had written."""
    from app.agent.reasoning_agent import ReasoningAgent

    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._enable_prompt_cache = True
    agent._prompt_cache_key = "admin:conv-1"

    args = agent._llm_args()
    assert args["prompt_cache"] is True
    assert args["prompt_cache_key"] == "admin:conv-1"
    # The Responses/Codex path spells the same thing differently.
    assert args["session_id"] == "admin:conv-1"


def test_no_key_without_a_conversation_to_key_on():
    """Better than inventing one: a per-turn random key is exactly the bug."""
    from app.agent.reasoning_agent import ReasoningAgent

    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._enable_prompt_cache = True

    args = agent._llm_args()
    assert args == {"prompt_cache": True}


def test_caching_off_sends_nothing():
    from app.agent.reasoning_agent import ReasoningAgent

    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._enable_prompt_cache = False
    agent._prompt_cache_key = "admin:conv-1"

    assert agent._llm_args() is None
