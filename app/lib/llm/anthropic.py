"""Anthropic LLM provider using the native Anthropic SDK.

Translates between Cremind's OpenAI-style message format and the
Anthropic Messages API, supporting both API key and bearer token auth.
"""

from typing import AsyncGenerator, Any, Dict, List, Optional, Union, cast
import json
import asyncio

import anthropic

from app.constants import ChatCompletionTypeEnum
from app.constants.status import Status
from app.lib.exception import AgentException
from app.types import FunctionCallingResponseType, ChatCompletionStreamResponseType
from app.utils import logger

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam
from openai.types import ResponseFormatJSONObject, ResponseFormatJSONSchema, ResponseFormatText

from .base import LLMProvider


def _convert_messages(messages: List[ChatCompletionMessageParam]):
    """Extract system prompt and convert OpenAI messages to Anthropic format.

    Returns (system_text, anthropic_messages).
    """
    system_parts: list[str] = []
    anthropic_msgs: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")

        if role == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(part.get("text", ""))
            continue

        if role == "assistant":
            content = msg.get("content")
            blocks: list[dict] = []

            # Text content
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            blocks.append({"type": "text", "text": part.get("text", "")})

            # Tool calls -> tool_use blocks
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": args,
                })

            if blocks:
                anthropic_msgs.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            # OpenAI tool result -> Anthropic tool_result
            tool_call_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            anthropic_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content if isinstance(content, str) else json.dumps(content),
                }],
            })
            continue

        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                anthropic_msgs.append({"role": "user", "content": content})
            elif isinstance(content, list):
                blocks = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            blocks.append({"type": "text", "text": part.get("text", "")})
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # Base64 image
                                media_type, _, b64 = url.partition(";base64,")
                                media_type = media_type.replace("data:", "")
                                blocks.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64,
                                    },
                                })
                if blocks:
                    anthropic_msgs.append({"role": "user", "content": blocks})
            continue

    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, anthropic_msgs


def _convert_tools(tools: Optional[List[ChatCompletionToolUnionParam]]) -> list[dict]:
    """Convert OpenAI tool definitions to Anthropic tool format."""
    if not tools:
        return []
    anthropic_tools = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
    return anthropic_tools


_CACHE_CONTROL = {"type": "ephemeral"}


def _system_param(system_text: str, cache_enabled: bool):
    """Return the Anthropic ``system`` param, adding a cache breakpoint when enabled.

    A plain string when caching is off; a single text block carrying
    ``cache_control`` when on, so Anthropic caches the system prefix.
    """
    if not cache_enabled:
        return system_text
    return [{"type": "text", "text": system_text, "cache_control": _CACHE_CONTROL}]


def _cache_last_tool(anthropic_tools: list[dict], cache_enabled: bool) -> list[dict]:
    """Mark the last tool with ``cache_control`` so the whole tools block is cached.

    Tools precede ``system`` in Anthropic's cache prefix, so a breakpoint on the
    final tool caches every tool definition. Returns the list unchanged when
    caching is off or there are no tools.
    """
    if not cache_enabled or not anthropic_tools:
        return anthropic_tools
    return anthropic_tools[:-1] + [{**anthropic_tools[-1], "cache_control": _CACHE_CONTROL}]


class AnthropicLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        model_name: str = "claude-sonnet-4-20250514",
        default_reasoning_effort: Optional[str] = None,
    ):
        if bearer_token:
            self.client = anthropic.AsyncAnthropic(
                api_key="placeholder",
                default_headers={"Authorization": f"Bearer {bearer_token}"},
            )
        elif api_key:
            self.client = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            raise ValueError("Either api_key or bearer_token must be provided")

        self.model_name = model_name
        self.default_reasoning_effort = default_reasoning_effort

    def _resolve_max_tokens(self, max_tokens: Optional[int]) -> int:
        """Return a sensible default max_tokens (required by the Anthropic API)."""
        if max_tokens:
            return max_tokens
        # Default 4096 for most models, higher for opus
        if "opus" in self.model_name:
            return 8192
        return 4096

    async def chat_completion_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
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
        logger.debug(f"[llm:anthropic] chat_completion_stream model={self.model_name}")
        cache_enabled = bool((args or {}).get("prompt_cache"))

        for attempt in range((retry or 0) + 1):
            content_total = ""
            tool_use_blocks: List[dict] = []
            current_tool: Optional[dict] = None
            input_tokens = 0
            output_tokens = 0
            finish_reason = None

            try:
                system_text, anthropic_messages = _convert_messages(messages)
                anthropic_tools = _convert_tools(tools)

                params: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": anthropic_messages,
                    "max_tokens": self._resolve_max_tokens(max_tokens),
                    "stream": True,
                }
                if system_text:
                    params["system"] = _system_param(system_text, cache_enabled)
                if temperature is not None:
                    params["temperature"] = temperature
                if top_p is not None:
                    params["top_p"] = top_p
                if stop:
                    params["stop_sequences"] = [stop]
                if anthropic_tools:
                    params["tools"] = _cache_last_tool(anthropic_tools, cache_enabled)
                    # Map OpenAI tool_choice to Anthropic
                    if tool_choice == "auto" or tool_choice is None:
                        params["tool_choice"] = {"type": "auto"}
                    elif tool_choice == "required":
                        params["tool_choice"] = {"type": "any"}
                    elif tool_choice == "none":
                        pass  # Don't send tool_choice
                    elif isinstance(tool_choice, dict) and "function" in tool_choice:
                        params["tool_choice"] = {
                            "type": "tool",
                            "name": tool_choice["function"].get("name", ""),
                        }

                async with self.client.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "message_start":
                            if hasattr(event, "message") and event.message.usage:
                                usage = event.message.usage
                                # Anthropic reports cached tokens separately from
                                # ``input_tokens``; fold them in so the total
                                # reflects the full prompt size, and log hits.
                                cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                                cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
                                input_tokens = usage.input_tokens + cache_read + cache_creation
                                if cache_enabled:
                                    logger.debug(
                                        f"[llm:anthropic] prompt cache: read={cache_read} "
                                        f"creation={cache_creation} uncached={usage.input_tokens}"
                                    )

                        elif event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool = {
                                    "id": block.id,
                                    "name": block.name,
                                    "arguments": "",
                                }

                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                content_total += delta.text
                                yield {
                                    "type": ChatCompletionTypeEnum.CONTENT,
                                    "data": delta.text,
                                }
                            elif delta.type == "input_json_delta" and current_tool is not None:
                                current_tool["arguments"] += delta.partial_json

                        elif event.type == "content_block_stop":
                            if current_tool is not None:
                                tool_use_blocks.append(current_tool)
                                current_tool = None

                        elif event.type == "message_delta":
                            if hasattr(event, "usage") and event.usage:
                                output_tokens = event.usage.output_tokens
                            if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                                sr = event.delta.stop_reason
                                if sr == "end_turn":
                                    finish_reason = "stop"
                                elif sr == "tool_use":
                                    finish_reason = "tool_calls"
                                elif sr == "max_tokens":
                                    finish_reason = "length"
                                elif sr == "stop_sequence":
                                    finish_reason = "stop"
                                else:
                                    finish_reason = sr

                # Emit tool calls
                if tool_use_blocks:
                    parsed_tools = []
                    for idx, tb in enumerate(tool_use_blocks):
                        try:
                            parsed_args = json.loads(tb["arguments"]) if tb["arguments"] else {}
                        except (json.JSONDecodeError, TypeError):
                            parsed_args = {}
                        parsed_tools.append({
                            "index": idx,
                            "id": tb["id"],
                            "name": tb["name"],
                            "arguments": parsed_args,
                        })

                    yield {
                        "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                        "data": {
                            "function": parsed_tools,
                            "outputToken": output_tokens,
                        },
                    }

                res: dict = {
                    "type": ChatCompletionTypeEnum.DONE,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "finish_reason": finish_reason,
                }
                if content_total:
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
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
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
        logger.debug(f"[llm:anthropic] chat_completion model={self.model_name}")
        cache_enabled = bool((args or {}).get("prompt_cache"))

        for attempt in range((retry or 0) + 1):
            try:
                system_text, anthropic_messages = _convert_messages(messages)
                anthropic_tools = _convert_tools(tools)

                params: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": anthropic_messages,
                    "max_tokens": self._resolve_max_tokens(max_tokens),
                }
                if system_text:
                    params["system"] = _system_param(system_text, cache_enabled)
                if temperature is not None:
                    params["temperature"] = temperature
                if top_p is not None:
                    params["top_p"] = top_p
                if stop:
                    params["stop_sequences"] = [stop]
                if anthropic_tools:
                    params["tools"] = _cache_last_tool(anthropic_tools, cache_enabled)
                    if tool_choice == "auto" or tool_choice is None:
                        params["tool_choice"] = {"type": "auto"}
                    elif tool_choice == "required":
                        params["tool_choice"] = {"type": "any"}
                    elif tool_choice == "none":
                        pass
                    elif isinstance(tool_choice, dict) and "function" in tool_choice:
                        params["tool_choice"] = {
                            "type": "tool",
                            "name": tool_choice["function"].get("name", ""),
                        }

                response = await self.client.messages.create(**params)

                # Process text content
                text_content = ""
                tool_use_blocks = []
                for block in response.content:
                    if block.type == "text":
                        text_content += block.text
                    elif block.type == "tool_use":
                        tool_use_blocks.append({
                            "id": block.id,
                            "name": block.name,
                            "arguments": block.input,
                        })

                # Check for json_schema response format
                response_format_type = None
                if response_format:
                    response_format_type = getattr(
                        response_format, 'type', None) or (
                        response_format.get('type') if isinstance(
                            response_format, dict) else None)

                function_calling: List[Dict[str, Any]] = []

                if text_content:
                    if response_format and response_format_type == "json_schema":
                        function_calling.append({
                            "name": "json_schema",
                            "arguments": text_content,
                        })
                    else:
                        yield {
                            "type": ChatCompletionTypeEnum.CONTENT,
                            "data": text_content,
                        }

                # Convert tool_use blocks to function calling format
                for tb in tool_use_blocks:
                    args = tb["arguments"]
                    function_calling.append({
                        "name": tb["name"],
                        "arguments": json.dumps(args) if isinstance(args, dict) else str(args),
                    })

                if function_calling:
                    parsed = []
                    for item in function_calling:
                        try:
                            parsed_args = json.loads(item["arguments"]) if isinstance(item["arguments"], str) else item["arguments"]
                        except (json.JSONDecodeError, TypeError):
                            parsed_args = {}
                        parsed.append({
                            "name": item["name"],
                            "arguments": parsed_args,
                        })
                    yield {
                        "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                        "data": {
                            "function": parsed,
                        },
                    }

                # Map stop reason
                finish_reason = "stop"
                if response.stop_reason == "tool_use":
                    finish_reason = "tool_calls"
                elif response.stop_reason == "max_tokens":
                    finish_reason = "length"

                input_tokens = None
                output_tokens = None
                if response.usage:
                    # Fold cached tokens into the input total (Anthropic reports
                    # them separately) and log cache activity.
                    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                    cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                    input_tokens = response.usage.input_tokens + cache_read + cache_creation
                    output_tokens = response.usage.output_tokens
                    if cache_enabled:
                        logger.debug(
                            f"[llm:anthropic] prompt cache: read={cache_read} "
                            f"creation={cache_creation} uncached={response.usage.input_tokens}"
                        )
                yield {
                    "type": ChatCompletionTypeEnum.DONE,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "finish_reason": finish_reason,
                    "data": text_content or None,
                }

                return  # success
            except Exception as err:
                last_error = err
                logger.error(err)
                if attempt == (retry or 0):
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * (attempt + 1))
