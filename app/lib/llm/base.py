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


# ---------------------------------------------------------------------------
# max_tokens vs max_completion_tokens
# ---------------------------------------------------------------------------
#
# OpenAI's GPT-5 and o-series reject ``max_tokens`` on Chat Completions with a
# 400 and tell you to send ``max_completion_tokens`` instead. Every provider
# that speaks the OpenAI wire format inherits the problem, including gateways
# that proxy those model ids under a vendor prefix. Two layers handle it:
#
#   1. :func:`uses_max_completion_tokens` recognizes the known families by name,
#      so the common case costs no wasted round-trip.
#   2. :func:`is_unsupported_max_tokens` + :func:`remember_max_completion_tokens`
#      adapt at runtime for ids we can't recognize (custom deployments, proxy
#      slugs, models that don't exist yet): the first request 400s, we memo the
#      endpoint+model, and every request after that uses the right name.

_MAX_COMPLETION_TOKEN_FAMILIES = ("gpt-5", "o1", "o3", "o4")

_UNSUPPORTED_MAX_TOKENS_MARKERS = (
    "max_completion_tokens",
    "'max_tokens' is not supported",
    '"max_tokens" is not supported',
)


def _bare_model_id(model_name: str) -> str:
    """Strip a gateway's ``vendor/`` prefix off a model id.

    LiteLLM/AI-Gateway route the same models as ``openai/gpt-5.4``, so family
    matching has to run on the last path segment.
    """
    return (model_name or "").strip().lower().rsplit("/", 1)[-1]


def uses_max_completion_tokens(model_name: str) -> bool:
    """Whether ``model_name`` belongs to a family that rejects ``max_tokens``."""
    model = _bare_model_id(model_name)
    return any(
        model == fam or model.startswith(fam + "-") or model.startswith(fam + ".")
        for fam in _MAX_COMPLETION_TOKEN_FAMILIES
    )


def is_unsupported_max_tokens(err: Any) -> bool:
    """Best-effort detection of the 'use max_completion_tokens instead' 400."""
    text = str(err or "").lower()
    return bool(text) and any(m in text for m in _UNSUPPORTED_MAX_TOKENS_MARKERS)


# Endpoints+models observed to require ``max_completion_tokens``. Module-level
# on purpose: ``create_llm_provider`` doesn't cache, so a fresh provider object
# is built per agent — a per-instance flag would re-pay the failed request on
# every turn. Keyed by (endpoint, model) since the same model id can behave
# differently behind different gateways.
_max_completion_tokens_seen: set[tuple[str, str]] = set()


def _memo_key(endpoint: Optional[str], model_name: str) -> tuple[str, str]:
    return ((endpoint or "").rstrip("/"), (model_name or "").strip().lower())


def needs_max_completion_tokens(
    endpoint: Optional[str], model_name: str, *, sniff_family: bool = False
) -> bool:
    """Whether this endpoint+model must be sent ``max_completion_tokens``.

    ``sniff_family`` should only be set by callers talking to **OpenAI itself**,
    where the family naming is authoritative. Gateways, proxies and local
    OpenAI-compatible servers (LiteLLM, AI-Gateway, Ollama, vLLM, …) accept
    ``max_tokens`` for the models they serve regardless of the id, and some
    reject the newer name — so for those we change nothing until the endpoint
    actually rejects a request, then adapt via the memo.
    """
    if sniff_family and uses_max_completion_tokens(model_name):
        return True
    return _memo_key(endpoint, model_name) in _max_completion_tokens_seen


def remember_max_completion_tokens(endpoint: Optional[str], model_name: str) -> None:
    """Record that this endpoint+model rejected ``max_tokens``."""
    _max_completion_tokens_seen.add(_memo_key(endpoint, model_name))


def apply_max_tokens(
    params: Dict[str, Any],
    max_tokens: Optional[int],
    *,
    endpoint: Optional[str],
    model_name: str,
    sniff_family: bool = False,
) -> None:
    """Set the max-output-tokens request param under whichever name applies."""
    if not max_tokens:
        return
    if needs_max_completion_tokens(endpoint, model_name, sniff_family=sniff_family):
        params["max_completion_tokens"] = max_tokens
    else:
        params["max_tokens"] = max_tokens


# ---------------------------------------------------------------------------
# reasoning_effort vs function tools
# ---------------------------------------------------------------------------
#
# Some models reject an explicit ``reasoning_effort`` when function tools are
# attached, on Chat Completions specifically:
#
#   Function tools with reasoning_effort are not supported for gpt-5.4-mini in
#   /v1/chat/completions. To use function tools, use /v1/responses or set
#   reasoning_effort to 'none'.
#
# Verified against the live API: with tools attached, *omitting* the parameter
# is accepted, as is ``'none'``. We omit rather than send ``'none'`` — omitting
# leaves the model reasoning at its own default, whereas ``'none'`` would switch
# reasoning off altogether. Omitting also avoids assuming ``'none'`` is a valid
# enum value on whatever endpoint we're actually talking to.
#
# Purely adaptive, with no model-name list: the restriction is per-model and
# per-endpoint (gpt-5.4-mini has it, its larger siblings may not), so we learn it
# from the one rejected request and memoize.

_TOOLS_REASONING_CONFLICT_MARKERS = (
    "function tools with reasoning_effort",
    "reasoning_effort are not supported",
)

_tools_reasoning_conflict_seen: set[tuple[str, str]] = set()


def is_tools_reasoning_effort_conflict(err: Any) -> bool:
    """Best-effort detection of the 'tools + reasoning_effort' 400."""
    text = str(err or "").lower()
    return bool(text) and any(m in text for m in _TOOLS_REASONING_CONFLICT_MARKERS)


def drops_reasoning_effort_with_tools(endpoint: Optional[str], model_name: str) -> bool:
    """Whether this endpoint+model rejects reasoning_effort alongside tools."""
    return _memo_key(endpoint, model_name) in _tools_reasoning_conflict_seen


def remember_tools_reasoning_conflict(endpoint: Optional[str], model_name: str) -> None:
    """Record that this endpoint+model rejected reasoning_effort with tools."""
    _tools_reasoning_conflict_seen.add(_memo_key(endpoint, model_name))


def _openai_cached_tokens(usage: Any, prompt: int) -> int:
    """Extract the cached-prompt-token count from an OpenAI-style ``usage`` object.

    The standard location is ``prompt_tokens_details.cached_tokens`` (OpenAI, Groq,
    xAI, Mistral, Qwen, MiniMax, Fireworks, LiteLLM, …). A few OpenAI-compatible
    providers report the same number under a different name, so we fall back to
    those when the standard field is absent/zero:

    - Together / Moonshot(Kimi): top-level ``usage.cached_tokens``
    - Others: top-level ``usage.prompt_cache_hit_tokens`` (with
      ``prompt_cache_miss_tokens`` being the uncached remainder). Kept as a
      provider-agnostic fallback — a user-defined ``custom:`` endpoint can
      still report cache hits this way.

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
