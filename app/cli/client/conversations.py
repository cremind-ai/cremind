"""Conversation endpoints — `/api/conversations*`, `/api/tasks/*/cancel`.

Mirrors `cli/internal/client/conversations.go`. The SSE stream itself is
opened via `Client.stream(conversation_stream_path(id))` from
`app.cli.streaming`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlencode

from app.cli.client._base import Client


@dataclass(frozen=True)
class SendMessageResponse:
    run_id: str
    conversation_id: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SendMessageResponse":
        return cls(
            run_id=str(d.get("run_id") or ""),
            conversation_id=str(d.get("conversation_id") or ""),
        )


async def list_conversations(
    client: Client,
    limit: int = 50,
    offset: int = 0,
    channel_type: str = "",
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if limit > 0:
        params["limit"] = limit
    if offset > 0:
        params["offset"] = offset
    if channel_type:
        params["channel_type"] = channel_type
    resp = await client.get_json("/api/conversations", params=params or None)
    if isinstance(resp, dict) and isinstance(resp.get("conversations"), list):
        return [c for c in resp["conversations"] if isinstance(c, dict)]
    return []


async def create_conversation(
    client: Client,
    title: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    resp = await client.post_json("/api/conversations", body)
    if isinstance(resp, dict) and isinstance(resp.get("conversation"), dict):
        return resp["conversation"]
    return {}


async def get_conversation(client: Client, conv_id: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/conversations/{quote(conv_id, safe='')}")
    return resp if isinstance(resp, dict) else {}


async def get_messages(
    client: Client,
    conv_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if limit > 0:
        params["limit"] = limit
    if offset > 0:
        params["offset"] = offset
    resp = await client.get_json(
        f"/api/conversations/{quote(conv_id, safe='')}/messages",
        params=params or None,
    )
    if isinstance(resp, dict) and isinstance(resp.get("messages"), list):
        return [m for m in resp["messages"] if isinstance(m, dict)]
    return []


async def send_message(
    client: Client,
    conv_id: str,
    text: str,
    reasoning: bool = True,
    mode: Optional[str] = None,
) -> SendMessageResponse:
    body: dict[str, Any] = {"text": text, "reasoning": reasoning}
    if mode:
        # Send both so a new CLI works against an old server (reads reasoning)
        # and a new server (reads mode); keep them coherent.
        body["mode"] = mode
        body["reasoning"] = mode != "instant"
    resp = await client.post_json(
        f"/api/conversations/{quote(conv_id, safe='')}/messages",
        body,
    )
    if not isinstance(resp, dict):
        raise RuntimeError("unexpected /messages response")
    return SendMessageResponse.from_dict(resp)


async def update_conversation(
    client: Client,
    conv_id: str,
    fields: dict[str, Any],
) -> None:
    await client.put_json(
        f"/api/conversations/{quote(conv_id, safe='')}",
        fields,
    )


async def delete_conversation(client: Client, conv_id: str) -> None:
    await client.delete(f"/api/conversations/{quote(conv_id, safe='')}")


async def delete_all_conversations(client: Client) -> int:
    """Returns the deleted_count from the server."""
    resp = await client.delete("/api/conversations")
    if isinstance(resp, dict):
        try:
            return int(resp.get("deleted_count") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


async def cancel_task(client: Client, task_id: str) -> bool:
    """Cancel an in-flight agent run by run_id (or task_id)."""
    resp = await client.post_json(f"/api/tasks/{quote(task_id, safe='')}/cancel")
    if isinstance(resp, dict):
        return bool(resp.get("cancelled") or False)
    return False


async def get_memory(client: Client, conv_id: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/conversations/{quote(conv_id, safe='')}/memory")
    return resp if isinstance(resp, dict) else {}


async def trigger_memory(client: Client, conv_id: str) -> bool:
    """Force a compaction now; returns whether the running summary changed."""
    resp = await client.post_json(f"/api/conversations/{quote(conv_id, safe='')}/memory/trigger")
    if isinstance(resp, dict):
        return bool(resp.get("compacted") or False)
    return False


async def get_usage(client: Client, conv_id: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/conversations/{quote(conv_id, safe='')}/usage")
    return resp if isinstance(resp, dict) else {}


def conversation_stream_path(conv_id: str) -> str:
    return f"/api/conversations/{quote(conv_id, safe='')}/stream"
