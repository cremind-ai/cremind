"""Group-chat endpoints — `/api/group-chats*`.

A *group chat* is one room several profiles share INSIDE Cremind: a human posts
once and every member agent decides for itself whether it was addressed. The
room's timeline is system-wide and membership is per profile. Not to be confused
with a channel group chat — one agent in a real Telegram/Slack group — which
lives under `cremind channels groups` (see `[cli]cremind group.md`).

The live timeline is opened via `Client.stream(group_stream_path(id, since))`
from `app.cli.commands.groups`; everything else here is plain JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from app.cli.client._base import Client


@dataclass(frozen=True)
class GroupChat:
    """One group room, as the server renders it."""

    id: str
    name: str
    members: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    #: Profiles whose seat is running a turn right now (GET one group only).
    thinking: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    #: `{sender_kind, sender_name, content, created_at}` of the newest post,
    #: or `{}` in an empty room (list responses only).
    last_message: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GroupChat":
        settings = d.get("settings") if isinstance(d.get("settings"), dict) else {}
        last = d.get("last_message") if isinstance(d.get("last_message"), dict) else {}
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            members=[str(m) for m in (d.get("members") or [])],
            settings=settings or {},
            thinking=[str(p) for p in (d.get("thinking") or [])],
            created_by=str(d.get("created_by") or ""),
            created_at=float(d.get("created_at") or 0),
            updated_at=float(d.get("updated_at") or 0),
            last_message=last or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "members": self.members,
            "settings": self.settings,
            "thinking": self.thinking,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message": self.last_message,
        }


@dataclass(frozen=True)
class GroupMessage:
    """One post in a group's timeline.

    `ordering` is the room's own counter (what `--after` and the stream's
    `since=` cursor take), and `hop` is the distance from the last human post —
    the loop guard that stops agents answering each other forever.
    """

    id: str
    group_id: str
    ordering: int
    sender_kind: str
    sender_name: str
    content: str
    hop: int = 0
    sender_profile: str = ""
    sender_identity: dict[str, Any] = field(default_factory=dict)
    delivered_to: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GroupMessage":
        identity = (
            d.get("sender_identity") if isinstance(d.get("sender_identity"), dict) else {}
        )
        metadata = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        return cls(
            id=str(d.get("id") or ""),
            group_id=str(d.get("group_id") or ""),
            ordering=_int_field(d.get("ordering")),
            sender_kind=str(d.get("sender_kind") or ""),
            sender_name=str(d.get("sender_name") or ""),
            content=str(d.get("content") or ""),
            hop=_int_field(d.get("hop")),
            sender_profile=str(d.get("sender_profile") or ""),
            sender_identity=identity or {},
            delivered_to=[str(p) for p in (d.get("delivered_to") or [])],
            metadata=metadata or {},
            created_at=float(d.get("created_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "ordering": self.ordering,
            "sender_kind": self.sender_kind,
            "sender_name": self.sender_name,
            "content": self.content,
            "hop": self.hop,
            "sender_profile": self.sender_profile,
            "sender_identity": self.sender_identity,
            "delivered_to": self.delivered_to,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


def _int_field(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _group_from_payload(resp: Any) -> GroupChat:
    """A `GroupChat` out of `{"group": {...}, "thinking": [...]}` or a bare dict."""
    if not isinstance(resp, dict):
        raise RuntimeError("unexpected /api/group-chats response")
    raw = resp.get("group") if isinstance(resp.get("group"), dict) else resp
    merged = dict(raw)
    # `thinking` rides alongside the group on the single-group GET, not inside it.
    if isinstance(resp.get("thinking"), list) and "thinking" not in raw:
        merged["thinking"] = resp["thinking"]
    return GroupChat.from_dict(merged)


async def list_groups(client: Client) -> list[GroupChat]:
    """Groups visible to the caller — every group for admin, own for a member."""
    resp = await client.get_json("/api/group-chats")
    raw: Any = resp
    if isinstance(resp, dict):
        raw = resp.get("groups")
    if not isinstance(raw, list):
        return []
    return [GroupChat.from_dict(g) for g in raw if isinstance(g, dict)]


async def create_group(
    client: Client,
    *,
    name: str,
    members: Optional[list[str]] = None,
    settings: Optional[dict[str, Any]] = None,
) -> GroupChat:
    """Create a room and seat every member profile in it (admin only)."""
    body: dict[str, Any] = {"name": name}
    if members:
        body["members"] = members
    if settings:
        body["settings"] = settings
    return _group_from_payload(await client.post_json("/api/group-chats", body))


async def resolve_group_id(client: Client, ref: str) -> str:
    """Find one group by id or by unique name, or raise with what to do next.

    A group id is a uuid nobody types, so every command takes the name as well.
    An ambiguous name raises rather than guessing: two rooms called "Ops" are
    two different sets of agents, and posting into the wrong one cannot be taken
    back.

    ``RuntimeError`` rather than a click exception so the caller can print one
    clean line — `graceful_errors` only covers config/API/network failures, and
    a mistyped name would otherwise end in a traceback.
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise RuntimeError("no group given")
    groups = await list_groups(client)
    for group in groups:
        if group.id == wanted:
            return group.id
    matches = [g for g in groups if g.name.strip().lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        ids = ", ".join(g.id for g in matches)
        raise RuntimeError(
            f"{len(matches)} groups are called {wanted!r}; use one of these "
            f"ids: {ids}"
        )
    raise RuntimeError(
        f"no group matches {wanted!r} — run `cremind group list`"
    )


async def get_group(client: Client, group_id: str) -> GroupChat:
    """One group with its members, settings and `thinking` profiles."""
    return _group_from_payload(
        await client.get_json(f"/api/group-chats/{quote(group_id, safe='')}")
    )


async def update_group(
    client: Client, group_id: str, fields: dict[str, Any],
) -> GroupChat:
    """PATCH `name` / `members` / `settings` (admin only).

    `settings` is replaced whole, not merged — read the group first and send
    back the full blob, or the knobs you did not mention fall back to defaults.
    """
    return _group_from_payload(
        await client.patch_json(f"/api/group-chats/{quote(group_id, safe='')}", fields)
    )


async def delete_group(client: Client, group_id: str) -> None:
    """Delete a group, its timeline, and every member's hidden seat."""
    await client.delete(f"/api/group-chats/{quote(group_id, safe='')}")


async def list_messages(
    client: Client, group_id: str, *, after: Optional[int] = None, limit: int = 100,
) -> list[GroupMessage]:
    """Timeline posts with `ordering > after`, oldest first.

    Omitting `after` is not "from the beginning": the server then returns its
    newest `limit` posts (still in reading order), which is what a client opening
    a long-running room wants. `ordering` starts at 0, so `after=-1` is what
    reads a room from its first post.
    """
    params: dict[str, Any] = {}
    if after is not None:
        params["after"] = after
    if limit > 0:
        params["limit"] = limit
    resp = await client.get_json(
        f"/api/group-chats/{quote(group_id, safe='')}/messages",
        params=params or None,
    )
    raw: Any = resp
    if isinstance(resp, dict):
        raw = resp.get("messages")
    if not isinstance(raw, list):
        return []
    return [GroupMessage.from_dict(m) for m in raw if isinstance(m, dict)]


async def post_message(
    client: Client,
    group_id: str,
    text: str,
    *,
    as_profile: str = "",
) -> GroupMessage:
    """Post into the room; every other member's agent gets its own turn.

    `as_profile` posts as that member agent (admin, or a member posting as
    itself) instead of as a human — it carries `hop=1`, so it counts against the
    loop guard.
    """
    body: dict[str, Any] = {"text": text}
    if as_profile:
        body["as_profile"] = as_profile
    resp = await client.post_json(
        f"/api/group-chats/{quote(group_id, safe='')}/messages", body,
    )
    if not isinstance(resp, dict):
        raise RuntimeError("unexpected /api/group-chats/{id}/messages response")
    raw = resp.get("message") if isinstance(resp.get("message"), dict) else resp
    return GroupMessage.from_dict(raw)


async def get_message_trace(
    client: Client, group_id: str, message_id: str,
) -> dict[str, Any]:
    """The thinking-process trace behind one agent post.

    Returns `{conversation_id, message: {id, content, thinking_steps, parts,
    provider, model, created_at}}` — the trace fields are nested under
    `message`, not flat; `parts` carries the turn's artefacts (terminals, files).
    The raw provider messages are never exposed, and only `admin` or the profile
    that wrote the post may read it: another tenant's turn is not room-readable.
    """
    resp = await client.get_json(
        f"/api/group-chats/{quote(group_id, safe='')}"
        f"/messages/{quote(message_id, safe='')}/trace"
    )
    return resp if isinstance(resp, dict) else {}


def group_stream_path(group_id: str, since: Optional[int] = None) -> str:
    """SSE path for a group's live timeline, replayed after `since` (ordering)."""
    path = f"/api/group-chats/{quote(group_id, safe='')}/stream"
    if since is not None:
        return f"{path}?since={since}"
    return path
