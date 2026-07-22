"""OpenAI Codex (ChatGPT backend) LLM provider.

When the OpenAI provider's active auth method is ``codex_oauth`` ("Sign in with
ChatGPT"), requests do **not** go to ``api.openai.com`` — they go to the ChatGPT
Codex backend at ``https://chatgpt.com/backend-api/codex/responses``, which
speaks the OpenAI **Responses** wire format (SSE only) and is billed against the
signed-in user's ChatGPT plan rather than per token.

This provider translates Cremind's chat.completions-shaped inputs into a
Responses request, streams the SSE response back into Cremind's yield contract
(the same chunk shapes :class:`app.lib.llm.openai.OpenAILLMProvider` emits), and
lazily fetches/refreshes the access token per request via
:func:`app.lib.llm.codex_auth.get_valid_access_token`.

It is a standalone provider (not an ``OpenAILLMProvider`` subclass) because the
wire format differs end to end; the closest in-repo model is
:mod:`app.lib.llm.anthropic`. httpx (a core dependency) is used directly rather
than the openai SDK, to keep exact control over the unofficial backend's
required headers and over mid-stream transient-error detection.

Backend quirks required by the ChatGPT Codex Responses endpoint: ``store``
forced false, ``stream`` forced true, non-empty ``instructions`` required,
``system`` role rewritten to ``developer``, server-generated item ids stripped,
sampling params dropped, and a strict body key allowlist applied last.

Known limitation: the encrypted reasoning items the backend returns are not
threaded back across turns (Cremind's persisted history is chat.completions
shaped and can't carry them). This costs cross-turn chain-of-thought continuity
only — correctness is unaffected because server ids are stripped, so the backend
never demands reasoning pairing.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union, cast

import httpx

from app.constants import ChatCompletionTypeEnum
from app.constants.status import Status
from app.lib.exception import AgentException
from app.types import ChatCompletionStreamResponseType
from app.utils import logger

from .base import LLMProvider, is_context_overflow
from .codex_auth import CodexCredentials, CodexReauthRequired, get_valid_access_token

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

_FIXED_HEADERS = {
    "originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.136.0",
    "Accept": "text/event-stream",
    "Content-Type": "application/json",
}

# The backend rejects an empty ``instructions`` field.
_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."

# Server-generated Responses item ids (store=false can't resolve them, so they
# must be stripped before the item is echoed back in ``input``).
_SERVER_ID_RE = re.compile(r"^(rs|fc|resp|msg)_")

# Only these keys survive to the wire (Responses API surface the backend accepts).
_ALLOWLIST = frozenset({
    "model", "input", "instructions", "tools", "tool_choice", "stream", "store",
    "reasoning", "service_tier", "include", "prompt_cache_key", "client_metadata", "text",
})

# Substrings in an SSE/HTTP error that mean "retry" rather than "fail".
_RETRYABLE_MARKERS = (
    "server_is_overloaded", "service_unavailable", "model_at_capacity",
    "selected model is at capacity",
)

_REAUTH_MESSAGE = (
    "OpenAI Codex sign-in has expired — re-authenticate via "
    "Settings → LLM Providers → OpenAI (Sign in with ChatGPT)."
)

_VALID_EFFORTS = ("low", "medium", "high")


class CodexTransientError(Exception):
    """A retryable Codex backend error (overload / capacity / 5xx)."""


# ── pure translation helpers ───────────────────────────────────────────────

def _text_of(content: Any) -> str:
    """Flatten a chat.completions ``content`` (str or parts list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                parts.append(part.get("text", ""))
        return "".join(parts)
    return ""


def _user_content_parts(content: Any) -> list[dict]:
    """Convert a user message's content into Responses ``input_*`` parts."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                parts.append({"type": "input_text", "text": part.get("text", "")})
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                detail = (part.get("image_url") or {}).get("detail") or "auto"
                if url:
                    parts.append({"type": "input_image", "image_url": url, "detail": detail})
            elif ptype == "input_audio":
                # The Codex backend has no audio input; the audio capability gate
                # (audio=false on every codex model) should keep audio away, so
                # drop defensively rather than send a part the API rejects.
                logger.warning("[llm:codex] dropping unsupported input_audio content part")
    if not parts:
        parts.append({"type": "input_text", "text": ""})
    return parts


def _convert_messages_to_input(messages: List[Any]) -> tuple[str, list[dict]]:
    """Return ``(instructions, input_items)`` for the Responses request.

    The first ``system`` message becomes ``instructions``; any further system
    messages become ``developer`` message items. Assistant ``tool_calls`` become
    ``function_call`` items; ``tool`` results become ``function_call_output``
    items.
    """
    instructions: Optional[str] = None
    items: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            text = _text_of(content)
            if instructions is None:
                instructions = text
            else:
                items.append({
                    "type": "message", "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                })
            continue

        if role == "user":
            items.append({"type": "message", "role": "user", "content": _user_content_parts(content)})
            continue

        if role == "assistant":
            text = _text_of(content)
            if text:
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in msg.get("tool_calls", []) or []:
                func = tc.get("function", {}) or {}
                args = func.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args)
                items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id", ""))[:64],
                    "name": func.get("name", ""),
                    "arguments": args,
                })
            continue

        if role == "tool":
            output = content if isinstance(content, str) else _text_of(content) or json.dumps(content)
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id", ""))[:64],
                "output": output,
            })
            continue

    # Strip server-generated ids and item_reference items; guarantee non-empty input.
    cleaned: list[dict] = []
    for item in items:
        if item.get("type") == "item_reference":
            continue
        if isinstance(item.get("id"), str) and _SERVER_ID_RE.match(item["id"]):
            item = {k: v for k, v in item.items() if k != "id"}
        cleaned.append(item)
    if not cleaned:
        cleaned.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": ""}]})

    return (instructions or _DEFAULT_INSTRUCTIONS), cleaned


def _convert_tools(tools: Optional[List[Any]]) -> list[dict]:
    """chat.completions nested tool shape → Responses flat function-tool shape."""
    if not tools:
        return []
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        func = tool.get("function", {}) or {}
        entry: dict = {
            "type": "function",
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "parameters": func.get("parameters") or {"type": "object", "properties": {}},
        }
        if "strict" in func:
            entry["strict"] = func.get("strict")
        out.append(entry)
    return out


def _convert_tool_choice(tool_choice: Optional[Union[str, Dict[str, Any]]]) -> Optional[Union[str, dict]]:
    if tool_choice in ("auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict) and "function" in tool_choice:
        return {"type": "function", "name": (tool_choice["function"] or {}).get("name", "")}
    return None


def _text_format(response_format: Any) -> Optional[dict]:
    """Map a chat.completions ``response_format`` onto Responses ``text.format``."""
    if not response_format:
        return None

    def _get(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    rf_type = _get(response_format, "type")
    if rf_type == "json_schema":
        js = _get(response_format, "json_schema") or {}
        fmt: dict = {
            "type": "json_schema",
            "name": _get(js, "name") or "response",
            "schema": _get(js, "schema") or {},
        }
        strict = _get(js, "strict")
        if strict is not None:
            fmt["strict"] = bool(strict)
        return {"format": fmt}
    if rf_type == "json_object":
        return {"format": {"type": "json_object"}}
    return None


def _resolve_effort(reasoning_effort: Optional[str], default: Optional[str]) -> str:
    """Resolve the reasoning effort to one of low/medium/high/none."""
    if reasoning_effort == "":  # instant mode: suppress reasoning
        return "none"
    eff = reasoning_effort if reasoning_effort is not None else default
    if eff in _VALID_EFFORTS:
        return eff
    if eff == "none":
        return "none"
    if eff == "minimal":
        return "low"
    return "medium"


def _build_request_body(
    model: str,
    messages: List[Any],
    tools: Optional[List[Any]],
    tool_choice: Optional[Union[str, Dict[str, Any]]],
    response_format: Any,
    reasoning_effort: Optional[str],
    default_reasoning_effort: Optional[str],
    session_id: str,
) -> dict:
    """Assemble the Responses request body, applying the key allowlist last."""
    instructions, input_items = _convert_messages_to_input(messages)
    body: dict = {
        "model": model,
        "input": input_items,
        "instructions": instructions,
        "stream": True,
        "store": False,
        "prompt_cache_key": session_id,
    }

    conv_tools = _convert_tools(tools)
    if conv_tools:
        body["tools"] = conv_tools
        tc = _convert_tool_choice(tool_choice)
        if tc is not None:
            body["tool_choice"] = tc

    text = _text_format(response_format)
    if text is not None:
        body["text"] = text

    effort = _resolve_effort(reasoning_effort, default_reasoning_effort)
    body["reasoning"] = {"effort": effort, "summary": "auto"}
    if effort != "none":
        body["include"] = ["reasoning.encrypted_content"]

    return {k: v for k, v in body.items() if k in _ALLOWLIST}


def _usage_breakdown(usage: Optional[dict]) -> Dict[str, Optional[int]]:
    """Adapt a Responses ``usage`` dict to Cremind's four-way token breakdown.

    (``base.openai_usage_breakdown`` reads attributes off a chat.completions
    usage *object*, so it can't consume this dict.)
    """
    if not usage:
        return {
            "input_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "output_tokens": None,
        }
    total_in = usage.get("input_tokens") or 0
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    cached = min(cached, total_in)
    return {
        "input_tokens": total_in - cached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": usage.get("output_tokens") or 0,  # includes reasoning tokens
    }


def _error_message(event: dict) -> str:
    err = event.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "codex error")
    if isinstance(err, str):
        return err
    return str(event.get("message") or "codex error")


def _classify_error(message: str) -> Exception:
    if any(m in message.lower() for m in _RETRYABLE_MARKERS):
        return CodexTransientError(message)
    return RuntimeError(message)


class CodexLLMProvider(LLMProvider):
    def __init__(
        self,
        config_storage,
        profile: Optional[str],
        model_name: str,
        default_reasoning_effort: Optional[str] = None,
    ):
        self.config_storage = config_storage
        self.profile = profile
        self.model_name = model_name
        self.default_reasoning_effort = default_reasoning_effort
        # Stable per-instance session id → stable prompt_cache_key across all
        # steps of one agent turn (the reasoning loop reuses this instance).
        self.session_id = str(uuid.uuid4())
        from tiktoken import encoding_for_model
        self.encoder = encoding_for_model("gpt-4o")  # token-count fallback
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
        )

    async def _headers(self) -> dict:
        creds: CodexCredentials = await get_valid_access_token(self.config_storage, self.profile)
        return {
            **_FIXED_HEADERS,
            "Authorization": f"Bearer {creds.access_token}",
            "chatgpt-account-id": creds.account_id,
            "session_id": self.session_id,
        }

    def _raise_http_error(self, status: int, text: str) -> None:
        if status == 401:
            raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, _REAUTH_MESSAGE)
        if status == 429:
            detail = "OpenAI Codex usage limit reached; try again later."
            try:
                err = (json.loads(text) or {}).get("error") or {}
                resets = err.get("resets_in_seconds") or err.get("resets_at")
                if resets:
                    detail = f"OpenAI Codex usage limit reached (resets: {resets})."
            except (ValueError, AttributeError):
                pass
            raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, detail)
        if status >= 500 or any(m in text.lower() for m in _RETRYABLE_MARKERS):
            raise CodexTransientError(f"codex backend {status}: {text[:200]}")
        # Other 4xx: let the outer handler route context-overflow specially.
        raise RuntimeError(f"codex request failed ({status}): {text[:300]}")

    async def _iter_events(self, headers: dict, body: dict) -> AsyncGenerator[dict, None]:
        async with self._client.stream("POST", CODEX_RESPONSES_URL, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                raw = await resp.aread()
                self._raise_http_error(resp.status_code, raw.decode("utf-8", "replace"))
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue

    def _reduce_event(self, event: dict, state: dict) -> Optional[str]:
        """Fold one SSE event into ``state``; return a text delta to emit, if any.

        Raises a classified error for backend error events.
        """
        et = event.get("type")
        if et == "response.output_text.delta":
            delta = event.get("delta") or ""
            if delta:
                state["content"] += delta
                return delta
            return None
        if et == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                args = item.get("arguments")
                state["calls"].append({
                    "index": len(state["calls"]),
                    "id": item.get("call_id") or item.get("id") or "",
                    "name": item.get("name") or "",
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                })
            return None
        if et in ("response.completed", "response.incomplete"):
            resp_obj = event.get("response") or {}
            state["usage"] = resp_obj.get("usage")
            if et == "response.incomplete":
                reason = (resp_obj.get("incomplete_details") or {}).get("reason")
                state["finish_reason"] = "length" if reason == "max_output_tokens" else "stop"
            return None
        if et == "response.failed":
            resp_obj = event.get("response") or {}
            raise _classify_error((resp_obj.get("error") or {}).get("message") or "response failed")
        if et == "error" or (et is None and "error" in event):
            raise _classify_error(_error_message(event))
        return None

    def _finalize_function_calls(self, calls: list[dict]) -> dict:
        """Build the streaming FUNCTION_CALLING chunk (openai.py shape)."""
        parsed = []
        tokens = 10
        for item in calls:
            try:
                parsed_args = json.loads(item["arguments"]) if item["arguments"] else {}
            except (json.JSONDecodeError, TypeError):
                parsed_args = {}
            parsed.append({
                "index": item["index"],
                "id": item["id"],
                "name": item["name"],
                "arguments": parsed_args,
            })
            tokens += len(self.encoder.encode(item["arguments"] or ""))
        return {
            "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
            "data": {"function": parsed, "outputToken": tokens},
        }

    async def chat_completion_stream(
        self,
        messages: List[Any],
        response_format: Any = None,
        tools: Optional[List[Any]] = None,
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
        logger.debug(f"[llm:codex] chat_completion_stream model={self.model_name}")
        session_id = (args or {}).get("session_id") or self.session_id
        for attempt in range((retry or 0) + 1):
            try:
                headers = await self._headers()
            except CodexReauthRequired as err:
                raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err) or _REAUTH_MESSAGE)
            body = _build_request_body(
                self.model_name, messages, tools, tool_choice, response_format,
                reasoning_effort, self.default_reasoning_effort, session_id,
            )
            state = {"content": "", "calls": [], "usage": None, "finish_reason": None}
            try:
                async for event in self._iter_events(headers, body):
                    delta = self._reduce_event(event, state)
                    if delta:
                        yield {"type": ChatCompletionTypeEnum.CONTENT, "data": delta}

                if state["calls"]:
                    yield cast(ChatCompletionStreamResponseType, self._finalize_function_calls(state["calls"]))

                finish_reason = state["finish_reason"] or ("tool_calls" if state["calls"] else "stop")
                res: dict = {
                    "type": ChatCompletionTypeEnum.DONE,
                    **_usage_breakdown(state["usage"]),
                    "finish_reason": finish_reason,
                }
                if state["content"]:
                    res["data"] = state["content"]
                yield cast(ChatCompletionStreamResponseType, res)
                return  # success
            except AgentException:
                raise  # 401 / 429 — not retryable
            except Exception as err:
                if is_context_overflow(err):
                    raise AgentException(Status.LLM_CONTEXT_OVERFLOW, str(err))
                if attempt == (retry or 0):
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * (attempt + 1))

    async def chat_completion(
        self,
        messages: List[Any],
        response_format: Any = None,
        tools: Optional[List[Any]] = None,
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
        """Non-streaming variant: the backend only streams, so consume the SSE
        and buffer, then emit the non-stream chunk shapes (matching openai.py)."""
        logger.debug(f"[llm:codex] chat_completion model={self.model_name}")
        session_id = (args or {}).get("session_id") or self.session_id

        response_format_type = None
        if response_format:
            response_format_type = getattr(response_format, "type", None) or (
                response_format.get("type") if isinstance(response_format, dict) else None
            )

        for attempt in range((retry or 0) + 1):
            try:
                headers = await self._headers()
            except CodexReauthRequired as err:
                raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err) or _REAUTH_MESSAGE)
            body = _build_request_body(
                self.model_name, messages, tools, tool_choice, response_format,
                reasoning_effort, self.default_reasoning_effort, session_id,
            )
            state = {"content": "", "calls": [], "usage": None, "finish_reason": None}
            try:
                async for event in self._iter_events(headers, body):
                    self._reduce_event(event, state)

                text_content = state["content"]
                function_calling: List[Dict[str, Any]] = []
                if text_content and response_format_type == "json_schema":
                    function_calling.append({"name": "json_schema", "arguments": text_content})
                elif text_content:
                    yield {"type": ChatCompletionTypeEnum.CONTENT, "data": text_content}

                for c in state["calls"]:
                    function_calling.append({"name": c["name"], "arguments": c["arguments"]})

                if function_calling:
                    parsed = []
                    for item in function_calling:
                        try:
                            parsed_args = json.loads(item["arguments"]) if isinstance(item["arguments"], str) else item["arguments"]
                        except (json.JSONDecodeError, TypeError):
                            parsed_args = {}
                        parsed.append({"name": item["name"], "arguments": parsed_args})
                    yield {
                        "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                        "data": {"function": parsed},
                    }

                finish_reason = state["finish_reason"] or ("tool_calls" if state["calls"] else "stop")
                yield {
                    "type": ChatCompletionTypeEnum.DONE,
                    **_usage_breakdown(state["usage"]),
                    "finish_reason": finish_reason,
                    "data": text_content or None,
                }
                return  # success
            except AgentException:
                raise
            except Exception as err:
                if is_context_overflow(err):
                    raise AgentException(Status.LLM_CONTEXT_OVERFLOW, str(err))
                if attempt == (retry or 0):
                    raise AgentException(Status.LLM_CHAT_COMPLETION_ERROR, str(err))
                await asyncio.sleep(0.5 * (attempt + 1))
