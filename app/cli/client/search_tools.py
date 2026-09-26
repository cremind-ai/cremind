"""Search-tools endpoints — which search sources the agent may use.

``/api/conversations/{id}/search-tools`` for an ordinary or event-run
conversation, ``/api/group-chats/{id}/search-tools`` for a group room's shared
choice. Both return the same state body::

    {"version", "enabled", "effective", "tools": [...],
     "pending_next_response", "cache_warning"}

A write sends ``{"version", "enabled"}``; a stale version comes back as a 409
whose body carries the current state under ``state``.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote

from app.cli.client._base import APIError, Client

# The four sources in their fixed priority order. The server is authoritative;
# this copy only lets the CLI reject a typo before a round trip (the CLI must
# not import server modules).
SEARCH_TOOL_IDS: tuple[str, ...] = (
    "documentation_search",
    "cremind_documentation_search",
    "memory_search",
    "web_search",
)


class VersionConflict(Exception):
    """The selection changed between our read and our write."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        super().__init__("the search tools were changed elsewhere")


def conversation_path(conversation_id: str) -> str:
    return f"/api/conversations/{quote(conversation_id, safe='')}/search-tools"


def group_path(group_id: str) -> str:
    return f"/api/group-chats/{quote(group_id, safe='')}/search-tools"


async def get_state(client: Client, path: str) -> dict[str, Any]:
    resp = await client.get_json(path)
    return resp if isinstance(resp, dict) else {}


async def put_state(
    client: Client, path: str, *, version: int, enabled: Optional[list[str]],
) -> dict[str, Any]:
    """Write a selection. ``enabled=None`` restores the defaults, ``[]`` turns
    every source off. Raises :class:`VersionConflict` on a 409."""
    try:
        resp = await client.put_json(path, {"version": int(version), "enabled": enabled})
    except APIError as exc:
        if exc.status == 409:
            # ``exc.body`` is the client's human rendering ("VersionConflict:
            # …"); the JSON the server sent, with its ``state``, is in ``raw``.
            try:
                body = json.loads(exc.raw.decode("utf-8")) if exc.raw else {}
            except (ValueError, UnicodeDecodeError):
                body = {}
            if isinstance(body, dict) and isinstance(body.get("state"), dict):
                raise VersionConflict(body["state"]) from exc
        raise
    return resp if isinstance(resp, dict) else {}
