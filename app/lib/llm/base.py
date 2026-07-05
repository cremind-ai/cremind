from __future__ import annotations

from typing import TYPE_CHECKING, AsyncGenerator, Any, Dict, List, Optional, Union
from abc import ABC, abstractmethod

from app.types import ChatCompletionStreamResponseType

# The OpenAI SDK isn't part of the thin-core install. Its types are
# referenced here only as type hints (PEP 563 stringifies them) so we
# can import them under TYPE_CHECKING and keep ``app.lib.llm.base``
# loadable without any extras group installed.
if TYPE_CHECKING:
    from openai.types import ResponseFormatJSONObject, ResponseFormatJSONSchema, ResponseFormatText
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam


_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "reduce the length",
    "context window",
    "exceeds the maximum",
)


def is_context_overflow(err: Any) -> bool:
    """Best-effort detection of a provider 'prompt too large for the window' error.

    Matches an exception or its message string across SDKs, so such errors can be
    routed to a clip-history-and-retry-once path instead of a doomed identical retry.
    """
    text = str(err or "").lower()
    return bool(text) and any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def _openai_cached_tokens(usage: Any, prompt: int) -> int:
    """Extract the cached-prompt-token count from an OpenAI-style ``usage`` object.

    The standard location is ``prompt_tokens_details.cached_tokens`` (OpenAI, Groq,
    xAI, Mistral, Qwen, MiniMax, Fireworks, OpenRouter, LiteLLM, …). A few
    OpenAI-compatible providers report the same number under a different name, so we
    fall back to those when the standard field is absent/zero:

    - Together / Moonshot(Kimi): top-level ``usage.cached_tokens``
    - DeepSeek: top-level ``usage.prompt_cache_hit_tokens`` (with
      ``prompt_cache_miss_tokens`` being the uncached remainder)

    (Gemini's ``cached_content_token_count`` lives in ``usageMetadata``, not the
    OpenAI usage object, so it isn't captured here — see provider notes.)
    """
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    if not cached:
        cached = (
            (getattr(usage, "cached_tokens", 0) or 0)
            or (getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        )
    return min(cached, prompt)  # guard against a malformed cached > prompt


def openai_usage_breakdown(usage: Any) -> Dict[str, Optional[int]]:
    """Normalize an OpenAI-style ``usage`` object into Cremind's token breakdown.

    OpenAI-compatible APIs report ``prompt_tokens`` as the *total* prompt size with
    the cached subset reported separately (see ``_openai_cached_tokens``). We split
    those apart so cost can be attributed accurately:

    - ``input_tokens``                -- uncached input (full price)
    - ``cache_read_input_tokens``     -- served from cache (discounted)
    - ``cache_creation_input_tokens`` -- always 0 (no separate cache-write on these APIs)
    - ``output_tokens``               -- completion tokens

    Returns all-``None`` when ``usage`` is missing.
    """
    if not usage:
        return {
            "input_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "output_tokens": None,
        }
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    cached = _openai_cached_tokens(usage, prompt)
    return {
        "input_tokens": prompt - cached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


def done_chunk_token_usage(response: Dict[str, Any]) -> Dict[str, int]:
    """Read the four-way token breakdown off a terminal ``DONE`` completion chunk.

    Every provider's ``chat_completion`` emits its usage on the terminal
    ``ChatCompletionTypeEnum.DONE`` chunk as four top-level int fields. This is the
    single place that names them, so direct ``chat_completion`` consumers (the
    skill-event gate, the ``documentation_search`` judge, ``image_understanding``)
    read usage identically instead of each re-listing the keys. Missing/``None``
    values coerce to 0.
    """
    return {
        "input_tokens": int(response.get("input_tokens") or 0),
        "cache_read_input_tokens": int(response.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(response.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(response.get("output_tokens") or 0),
    }


class LLMProvider(ABC):
    provider_name: str = ""

    @property
    def model_label(self) -> str:
        """Human-readable label combining provider and model, e.g. 'Groq GPT-OSS-120B'."""
        name = getattr(self, "model_name", "")
        if self.provider_name and name:
            return f"{self.provider_name.capitalize()} {name}"
        return name or "unknown"

    @abstractmethod
    def chat_completion_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        # "auto" | "none" | "required" | ChatCompletionNamedToolChoice
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,  # "low" | "medium" | "high"
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        pass

    @abstractmethod
    def chat_completion(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        # "auto" | "none" | "required" | ChatCompletionNamedToolChoice
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,  # "low" | "medium" | "high"
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        pass
