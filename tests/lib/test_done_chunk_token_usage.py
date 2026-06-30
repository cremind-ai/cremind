"""Tests for ``done_chunk_token_usage`` (``app.lib.llm.base``).

The single place that names the four token fields carried on a provider's
terminal ``DONE`` chunk. Direct ``chat_completion`` consumers (the skill-event
gate, the documentation_search judge, image_understanding) read usage through it.
"""

from __future__ import annotations

from app.lib.llm.base import done_chunk_token_usage

_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def test_reads_all_four_fields():
    res = done_chunk_token_usage({
        "type": "ignored",
        "input_tokens": 100,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
        "output_tokens": 8,
    })
    assert res == {
        "input_tokens": 100,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
        "output_tokens": 8,
    }


def test_missing_and_none_coerce_to_zero():
    # Empty chunk → all-zero baseline (the shape callers init with).
    assert done_chunk_token_usage({}) == {k: 0 for k in _KEYS}
    # Partial + explicit None both coerce to 0.
    res = done_chunk_token_usage({"input_tokens": 7, "output_tokens": None})
    assert res["input_tokens"] == 7
    assert res["output_tokens"] == 0
    assert res["cache_read_input_tokens"] == 0


def test_always_returns_ints():
    res = done_chunk_token_usage({"input_tokens": "12"})
    assert res["input_tokens"] == 12
    assert all(isinstance(v, int) for v in res.values())
