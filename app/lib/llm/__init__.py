"""LLM provider surface.

Only ``LLMProvider`` (the abstract base) and the factory helpers are
re-exported here. Concrete provider classes (``OpenAILLMProvider``,
``GroqLLMProvider``, ``AnthropicLLMProvider``, ...) are not — each one
imports its own heavyweight SDK at module load time, and we don't want a
``from app.lib.llm import LLMProvider`` to drag in every SDK we know
about.

To instantiate a provider, use :func:`create_llm_provider` from
:mod:`app.lib.llm.factory`; it does a lazy ``from .openai import ...``
inside the matching branch so only the SDK for the selected provider is
loaded.
"""

from .base import LLMProvider
from .__main__ import (
    llm_quick_prompt,
    llm_stream_quick_prompt,
)
from .factory import create_llm_provider, SUPPORTED_LLM_PROVIDERS

__all__ = [
    "llm_quick_prompt",
    "llm_stream_quick_prompt",
    "LLMProvider",
    "create_llm_provider",
    "SUPPORTED_LLM_PROVIDERS",
]
