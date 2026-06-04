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

from .base import LLMProvider


class VllmLLMProvider(LLMProvider):
    def __init__(self, api_key: str, model_name: str, default_reasoning_effort: Optional[str] = None):
        self.openai = AsyncOpenAI(api_key=api_key, base_url="http://localhost:8000/v1")
        self.model_name = model_name
        self.default_reasoning_effort = default_reasoning_effort
        self.encoder = encoding_for_model("gpt-4o")  # Fallback

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
        last_error: Any = None

        for attempt in range((retry or 0) + 1):
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
                if temperature:
                    params["temperature"] = temperature
                if top_p:
                    params["top_p"] = top_p
                if stop:
                    params["stop"] = stop
                if max_tokens:
                    params["max_tokens"] = max_tokens
                if response_format:
                    params["response_format"] = response_format
                if self.default_reasoning_effort is not None:
                    _re = reasoning_effort if reasoning_effort is not None else self.default_reasoning_effort
                    if _re:
                        params["reasoning_effort"] = _re
                if tools:
                    params["tool_choice"] = tool_choice or "auto"
                    params["tools"] = tools
                if parallel_tool_calls is not None:
                    params["parallel_tool_calls"] = parallel_tool_calls

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
                    "input_tokens": usage.prompt_tokens if usage else None,
                    "output_tokens": usage.completion_tokens if usage else None,
                    "finish_reason": finish_reason,
                }
                if len(content_total) > 0:
                    res["data"] = content_total
                yield cast(ChatCompletionStreamResponseType, res)

                return  # success
            except Exception as err:
                last_error = err
                if attempt == (retry or 0):
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * (attempt + 1))

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
        last_error: Any = None

        for attempt in range((retry or 0) + 1):
            function_calling: List[Dict[str, str]] = []
            function_calling_tokens = 0
            try:
                params = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                }
                if temperature:
                    params["temperature"] = temperature
                if top_p:
                    params["top_p"] = top_p
                if stop:
                    params["stop"] = stop
                if max_tokens:
                    params["max_tokens"] = max_tokens
                if response_format:
                    params["response_format"] = response_format
                if self.default_reasoning_effort is not None:
                    _re = reasoning_effort if reasoning_effort is not None else self.default_reasoning_effort
                    if _re:
                        params["reasoning_effort"] = _re
                if tools:
                    params["tool_choice"] = tool_choice or "auto"
                    params["tools"] = tools
                if parallel_tool_calls is not None:
                    params["parallel_tool_calls"] = parallel_tool_calls

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
                    "input_tokens": response.usage.prompt_tokens if response.usage else None,
                    "output_tokens": response.usage.completion_tokens if response.usage else None,
                    "finish_reason": response.choices[0].finish_reason if len(response.choices) > 0 else None,
                    "data": response.choices[0].message.content,
                }

                return  # success
            except Exception as err:
                last_error = err
                logger.error(err)
                if attempt == (retry or 0):
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * (attempt + 1))
