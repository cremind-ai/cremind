from typing import AsyncGenerator, Any, Dict, List, Optional, Union, cast, AsyncIterator
import json
import asyncio
from tiktoken import encoding_for_model

import openai
from openai import AsyncOpenAI

from app.constants import ChatCompletionTypeEnum
from app.constants.status import Status
from app.lib.exception import AgentException

from app.types import FunctionCallingResponseType, ChatCompletionStreamResponseType
from app.utils import logger

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam, ChatCompletionNamedToolChoiceParam
from openai.types import ResponseFormatJSONObject, ResponseFormatJSONSchema, ResponseFormatText

from .base import (
    LLMProvider,
    apply_max_tokens,
    drops_reasoning_effort_with_tools,
    is_context_overflow,
    is_tools_reasoning_effort_conflict,
    is_unsupported_max_tokens,
    openai_usage_breakdown,
    remember_max_completion_tokens,
    remember_tools_reasoning_conflict,
)


class OpenAILLMProvider(LLMProvider):
    # Class-level defaults so subclasses that build their own client without
    # calling super().__init__ (GitHubCopilotLLMProvider) still resolve these.
    base_url: Optional[str] = None
    _max_tokens_endpoint: str = "openai"
    _sniff_max_completion_tokens: bool = False

    def __init__(self, api_key: str, model_name: str, default_reasoning_effort: Optional[str] = None, base_url: Optional[str] = None):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.openai = AsyncOpenAI(**kwargs)
        self.model_name = model_name
        # Kept so the max_tokens/max_completion_tokens memo can be keyed per
        # endpoint — the same model id can behave differently behind a gateway.
        # No base_url means we're talking to OpenAI itself, which is the only
        # case where the gpt-5/o-series naming is authoritative enough to act on
        # before the endpoint has actually rejected anything.
        self.base_url = base_url
        self._max_tokens_endpoint = base_url or "openai"
        self._sniff_max_completion_tokens = base_url is None
        self.default_reasoning_effort = default_reasoning_effort
        self.encoder = encoding_for_model("gpt-4o")  # Fallback

    def _prompt_cache_key(self, args: Optional[Dict[str, Any]]) -> Optional[str]:
        """The caller's cache-routing key, if this endpoint understands one.

        OpenAI caches long prompt prefixes automatically, but routes on an
        opaque key so that requests sharing a prefix land on the same cache;
        without one the routing is by organisation and effectively a coin toss
        for a busy account. Sending it turns "18k tokens, 0 cached, every turn"
        into a hit from the second turn on.

        Restricted to OpenAI proper. ``base_url`` means some other
        OpenAI-compatible server — Groq, Ollama, vLLM, a gateway — and several
        of those reject a request carrying a parameter they do not know rather
        than ignoring it, which would break every call instead of missing a
        cache.
        """
        if self.base_url is not None:
            return None
        key = (args or {}).get("prompt_cache_key")
        return str(key) if key else None

    async def chat_completion_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        tool_choice: Optional[Union[str, ChatCompletionNamedToolChoiceParam]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        logger.debug(f"[llm:openai] chat_completion_stream model={self.model_name}")
        max_attempts = (retry or 0) + 1
        attempt = 0
        renamed_max_tokens = False
        dropped_reasoning_effort = False
        while True:
            function_calling: List[FunctionCallingResponseType] = []
            content_total = ""
            usage = None
            finish_reason = None
            try:
                params = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if temperature is not None:
                    params["temperature"] = temperature
                if top_p:
                    params["top_p"] = top_p
                if stop:
                    params["stop"] = stop
                apply_max_tokens(
                    params, max_tokens,
                    endpoint=self._max_tokens_endpoint,
                    model_name=self.model_name,
                    sniff_family=self._sniff_max_completion_tokens,
                )
                if response_format:
                    params["response_format"] = response_format
                if self.default_reasoning_effort is not None:
                    _re = reasoning_effort if reasoning_effort is not None else self.default_reasoning_effort
                    # Some models reject an explicit effort when tools are
                    # attached; omitting it is accepted and leaves the model
                    # reasoning at its own default.
                    if tools and drops_reasoning_effort_with_tools(
                        self._max_tokens_endpoint, self.model_name
                    ):
                        _re = None
                    if _re:
                        params["reasoning_effort"] = _re
                if tools:
                    params["tool_choice"] = tool_choice or "auto"
                    params["tools"] = tools
                if parallel_tool_calls is not None:
                    params["parallel_tool_calls"] = parallel_tool_calls
                cache_key = self._prompt_cache_key(args)
                if cache_key:
                    params["prompt_cache_key"] = cache_key

                response = await self.openai.chat.completions.create(**params)

                async for chunk in response:
                    if len(chunk.choices) > 0 and chunk.choices[0].delta:
                        if chunk.choices[0].delta.content:
                            content_total += chunk.choices[0].delta.content
                            yield {
                                "type": ChatCompletionTypeEnum.CONTENT,
                                "data": chunk.choices[0].delta.content,
                            }
                        if chunk.choices[0].delta.tool_calls:
                            tool_call = chunk.choices[0].delta.tool_calls[0]
                            if tool_call.type == "function":
                                function_calling.append({
                                    "name": tool_call.function.name,
                                    "index": tool_call.index,
                                    "id": tool_call.id,
                                    "arguments": "",
                                })
                            function_calling[tool_call.index]["arguments"] += tool_call.function.arguments
                    if len(chunk.choices) > 0 and chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                    if chunk.usage:
                        usage = chunk.usage

                parsed_function_calling = [
                    {
                        "index": item["index"],
                        "id": item["id"],
                        "name": item["name"],
                        "arguments": json.loads(item["arguments"]),
                    }
                    for item in function_calling
                ]

                if function_calling:
                    function_calling_tokens = 10
                    for item in function_calling:
                        arg_tokens = len(self.encoder.encode(item["arguments"]))
                        function_calling_tokens += arg_tokens
                    yield {
                        "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                        "data": {
                            "function": parsed_function_calling,
                            "outputToken": function_calling_tokens,
                        },
                    }

                res = {
                    "type": ChatCompletionTypeEnum.DONE,
                    **openai_usage_breakdown(usage),
                    "finish_reason": finish_reason,
                }
                if len(content_total) > 0:
                    res["data"] = content_total
                yield cast(ChatCompletionStreamResponseType, res)

                return  # success
            except Exception as err:
                if is_context_overflow(err):
                    # Retrying an oversized prompt is futile — surface it distinctly
                    # so the reasoning loop can clip history and retry once.
                    raise AgentException(Status.LLM_CONTEXT_OVERFLOW, str(err))
                if is_unsupported_max_tokens(err) and not renamed_max_tokens:
                    # GPT-5 / o-series (and anything proxying them) want
                    # `max_completion_tokens`. Memo it so later requests get it
                    # right first time, then retry now — this one is on the
                    # house, since it isn't the failure the caller budgeted for.
                    renamed_max_tokens = True
                    remember_max_completion_tokens(
                        self._max_tokens_endpoint, self.model_name
                    )
                    logger.info(
                        f"[llm:openai] {self.model_name} rejected max_tokens; "
                        f"retrying with max_completion_tokens"
                    )
                    continue
                if is_tools_reasoning_effort_conflict(err) and not dropped_reasoning_effort:
                    # This model won't take an explicit reasoning_effort while
                    # function tools are attached. Memo it and retry without the
                    # parameter — also off-budget, since the identical request
                    # would fail identically. Warn rather than info: the user's
                    # configured effort is being silently dropped for tool calls.
                    dropped_reasoning_effort = True
                    remember_tools_reasoning_conflict(
                        self._max_tokens_endpoint, self.model_name
                    )
                    logger.warning(
                        f"[llm:openai] {self.model_name} rejects reasoning_effort "
                        f"when function tools are attached; dropping it for "
                        f"tool-bearing requests (the model still reasons at its "
                        f"own default). Use /v1/responses for explicit control."
                    )
                    continue
                attempt += 1
                if attempt >= max_attempts:
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * attempt)

    async def chat_completion(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        tool_choice: Optional[Union[str, ChatCompletionNamedToolChoiceParam]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        logger.debug(f"[llm:openai] chat_completion model={self.model_name}")
        max_attempts = (retry or 0) + 1
        attempt = 0
        renamed_max_tokens = False
        dropped_reasoning_effort = False
        while True:
            function_calling: List[Dict[str, str]] = []
            function_calling_tokens = 0
            try:
                params = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                }
                if temperature is not None:
                    params["temperature"] = temperature
                if top_p:
                    params["top_p"] = top_p
                if stop:
                    params["stop"] = stop
                apply_max_tokens(
                    params, max_tokens,
                    endpoint=self._max_tokens_endpoint,
                    model_name=self.model_name,
                    sniff_family=self._sniff_max_completion_tokens,
                )
                if response_format:
                    params["response_format"] = response_format
                if self.default_reasoning_effort is not None:
                    _re = reasoning_effort if reasoning_effort is not None else self.default_reasoning_effort
                    # Some models reject an explicit effort when tools are
                    # attached; omitting it is accepted and leaves the model
                    # reasoning at its own default.
                    if tools and drops_reasoning_effort_with_tools(
                        self._max_tokens_endpoint, self.model_name
                    ):
                        _re = None
                    if _re:
                        params["reasoning_effort"] = _re
                if tools:
                    params["tool_choice"] = tool_choice or "auto"
                    params["tools"] = tools
                if parallel_tool_calls is not None:
                    params["parallel_tool_calls"] = parallel_tool_calls
                cache_key = self._prompt_cache_key(args)
                if cache_key:
                    params["prompt_cache_key"] = cache_key

                response = await self.openai.chat.completions.create(**params)

                if response.choices[0].message.content:
                    response_format_type = getattr(
                        response_format, 'type', None) or (
                        response_format.get('type') if isinstance(
                            response_format, dict) else None)
                    if response_format and response_format_type == "json_schema":
                        function_calling.append({
                            "name": "json_schema",
                            "arguments": response.choices[0].message.content,
                        })
                    else:
                        yield {
                            "type": ChatCompletionTypeEnum.CONTENT,
                            "data": response.choices[0].message.content,
                        }

                if response.choices[0].message.tool_calls:
                    for tool_call in response.choices[0].message.tool_calls:
                        if tool_call.type == "function":
                            function_calling.append({
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            })

                parsed_function_calling = [
                    {
                        "name": item["name"],
                        "arguments": json.loads(item["arguments"]),
                    }
                    for item in function_calling
                ]

                if function_calling:
                    function_calling_tokens = 10
                    for item in function_calling:
                        arg_tokens = len(self.encoder.encode(item["arguments"]))
                        function_calling_tokens += arg_tokens
                    yield {
                        "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                        "output_tokens": function_calling_tokens,
                        "data": {
                            "function": parsed_function_calling,
                        },
                    }

                yield {
                    "type": ChatCompletionTypeEnum.DONE,
                    **openai_usage_breakdown(response.usage),
                    "finish_reason": response.choices[0].finish_reason if len(response.choices) > 0 else None,
                    "data": response.choices[0].message.content,
                }

                return  # success
            except Exception as err:
                logger.error(err)
                if is_unsupported_max_tokens(err) and not renamed_max_tokens:
                    # See the matching branch in chat_completion_stream.
                    renamed_max_tokens = True
                    remember_max_completion_tokens(
                        self._max_tokens_endpoint, self.model_name
                    )
                    logger.info(
                        f"[llm:openai] {self.model_name} rejected max_tokens; "
                        f"retrying with max_completion_tokens"
                    )
                    continue
                if is_tools_reasoning_effort_conflict(err) and not dropped_reasoning_effort:
                    # This model won't take an explicit reasoning_effort while
                    # function tools are attached. Memo it and retry without the
                    # parameter — also off-budget, since the identical request
                    # would fail identically. Warn rather than info: the user's
                    # configured effort is being silently dropped for tool calls.
                    dropped_reasoning_effort = True
                    remember_tools_reasoning_conflict(
                        self._max_tokens_endpoint, self.model_name
                    )
                    logger.warning(
                        f"[llm:openai] {self.model_name} rejects reasoning_effort "
                        f"when function tools are attached; dropping it for "
                        f"tool-bearing requests (the model still reasons at its "
                        f"own default). Use /v1/responses for explicit control."
                    )
                    continue
                attempt += 1
                if attempt >= max_attempts:
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * attempt)
