"""Token accounting must distinguish uncached input from cached reads/writes.

Covers the shared OpenAI-style breakdown helper, the per-provider DONE-chunk
emission (OpenAI splits ``prompt_tokens``; Anthropic no longer folds cache
tokens into ``input_tokens``), and the CLI display.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.lib.llm.base import openai_usage_breakdown  # noqa: E402
from app.lib.llm.openai import OpenAILLMProvider  # noqa: E402
from app.lib.llm.anthropic import AnthropicLLMProvider  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────

async def _collect(agen):
    out = []
    try:
        async for item in agen:
            out.append(item)
    except Exception:
        pass
    return out


def _done(chunks):
    return next(c for c in chunks if c.get("type") == ChatCompletionTypeEnum.DONE)


# ── openai_usage_breakdown (pure) ──────────────────────────────────────────

def test_breakdown_splits_cached_subset_from_prompt():
    usage = SimpleNamespace(
        prompt_tokens=2006,
        completion_tokens=300,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1920),
    )
    b = openai_usage_breakdown(usage)
    assert b == {
        "input_tokens": 86,                 # 2006 - 1920 uncached
        "cache_read_input_tokens": 1920,    # the cached subset
        "cache_creation_input_tokens": 0,   # no separate write on OpenAI-style APIs
        "output_tokens": 300,
    }


def test_breakdown_without_details_has_no_cache():
    usage = SimpleNamespace(prompt_tokens=500, completion_tokens=40, prompt_tokens_details=None)
    b = openai_usage_breakdown(usage)
    assert b["input_tokens"] == 500
    assert b["cache_read_input_tokens"] == 0
    assert b["cache_creation_input_tokens"] == 0


def test_breakdown_none_usage_all_none():
    b = openai_usage_breakdown(None)
    assert b == {
        "input_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "output_tokens": None,
    }


def test_breakdown_deepseek_style_cache_hit_field():
    # DeepSeek reports cached prompt tokens via top-level prompt_cache_hit_tokens
    # (no prompt_tokens_details).
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50, prompt_cache_hit_tokens=800)
    b = openai_usage_breakdown(usage)
    assert b["input_tokens"] == 200
    assert b["cache_read_input_tokens"] == 800
    assert b["cache_creation_input_tokens"] == 0


def test_breakdown_together_style_toplevel_cached_tokens():
    # Together / Moonshot report cached tokens as a top-level usage.cached_tokens.
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=40, cached_tokens=600)
    b = openai_usage_breakdown(usage)
    assert b["input_tokens"] == 400
    assert b["cache_read_input_tokens"] == 600


def test_breakdown_details_takes_precedence_over_fallback():
    # When the standard field is present and non-zero, it wins over fallbacks.
    usage = SimpleNamespace(
        prompt_tokens=1000, completion_tokens=10,
        prompt_tokens_details=SimpleNamespace(cached_tokens=300),
        cached_tokens=999,  # should be ignored
    )
    b = openai_usage_breakdown(usage)
    assert b["cache_read_input_tokens"] == 300
    assert b["input_tokens"] == 700


# ── OpenAI provider emission ───────────────────────────────────────────────

def _make_openai_provider(response):
    class _Completions:
        async def create(self, **kwargs):
            return response

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    p = OpenAILLMProvider.__new__(OpenAILLMProvider)
    p.model_name = "gpt-5.4"
    p.default_reasoning_effort = None
    p.encoder = None
    p.openai = _Client()
    return p


def test_openai_done_chunk_splits_cached():
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="hi", tool_calls=None),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=2006, completion_tokens=300,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1920),
        ),
    )
    p = _make_openai_provider(response)
    done = _done(asyncio.run(_collect(p.chat_completion(messages=[{"role": "user", "content": "x"}], retry=0))))
    assert done["input_tokens"] == 86
    assert done["cache_read_input_tokens"] == 1920
    assert done["cache_creation_input_tokens"] == 0
    assert done["output_tokens"] == 300


# ── Anthropic provider emission (no folding) ───────────────────────────────

def _make_anthropic_provider(response):
    class _Messages:
        async def create(self, **kwargs):
            return response

    class _Client:
        messages = _Messages()

    p = AnthropicLLMProvider.__new__(AnthropicLLMProvider)
    p.model_name = "claude-sonnet-4-6"
    p.default_reasoning_effort = None
    p.client = _Client()
    return p


def test_anthropic_done_chunk_keeps_cache_unfolded():
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=50,
            cache_read_input_tokens=1800,
            cache_creation_input_tokens=5120,
            output_tokens=503,
        ),
    )
    p = _make_anthropic_provider(response)
    done = _done(asyncio.run(_collect(p.chat_completion(messages=[{"role": "user", "content": "x"}], retry=0))))
    # input_tokens must be the UNCACHED remainder only — not folded with cache.
    assert done["input_tokens"] == 50
    assert done["cache_read_input_tokens"] == 1800
    assert done["cache_creation_input_tokens"] == 5120
    assert done["output_tokens"] == 503


def test_anthropic_done_chunk_no_cache_is_zero():
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),  # no cache_* attrs
    )
    p = _make_anthropic_provider(response)
    done = _done(asyncio.run(_collect(p.chat_completion(messages=[{"role": "user", "content": "x"}], retry=0))))
    assert done["input_tokens"] == 120
    assert done["cache_read_input_tokens"] == 0
    assert done["cache_creation_input_tokens"] == 0


# ── CLI display ────────────────────────────────────────────────────────────

def test_extract_token_usage_shows_cached_segment():
    from app.cli.tui.renderer import extract_token_usage

    ev = SimpleNamespace(type="token_usage", data={"data": {"token_usage": {
        "input_tokens": 1200,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 450,
    }}})
    # total = 1200 + 8000 + 0 + 450 = 9650; cached = 8000
    assert extract_token_usage(ev) == "tokens: 9650  (in 1200, cached 8000 / out 450)"


def test_extract_token_usage_legacy_record_unchanged():
    from app.cli.tui.renderer import extract_token_usage

    ev = SimpleNamespace(type="token_usage", data={"data": {"token_usage": {
        "input_tokens": 100,
        "output_tokens": 50,
    }}})
    # No cache keys -> renders exactly like before.
    assert extract_token_usage(ev) == "tokens: 150  (in 100 / out 50)"
