"""Channels endpoints — `/api/channels*`.

Mirrors `cli/internal/client/channels.go`. The pairing SSE stream is consumed
by the `channels pair` command (Phase 4); list/catalog/create/delete are
straightforward JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from app.cli.client._base import Client


@dataclass(frozen=True)
class Channel:
    id: str
    profile: str
    channel_type: str
    mode: str
    auth_mode: str
    response_mode: str
    enabled: bool
    status: str
    config: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Channel":
        config = d.get("config") if isinstance(d.get("config"), dict) else {}
        state = d.get("state") if isinstance(d.get("state"), dict) else {}
        return cls(
            id=str(d.get("id") or ""),
            profile=str(d.get("profile") or ""),
            channel_type=str(d.get("channel_type") or ""),
            mode=str(d.get("mode") or ""),
            auth_mode=str(d.get("auth_mode") or ""),
            response_mode=str(d.get("response_mode") or ""),
            enabled=bool(d.get("enabled") or False),
            status=str(d.get("status") or ""),
            config=config or {},
            state=state or {},
            created_at=float(d.get("created_at") or 0),
            updated_at=float(d.get("updated_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "channel_type": self.channel_type,
            "mode": self.mode,
            "auth_mode": self.auth_mode,
            "response_mode": self.response_mode,
            "enabled": self.enabled,
            "status": self.status,
            "config": self.config,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


async def list_channels(client: Client) -> list[Channel]:
    resp = await client.get_json("/api/channels")
    if isinstance(resp, dict) and isinstance(resp.get("channels"), list):
        return [Channel.from_dict(c) for c in resp["channels"] if isinstance(c, dict)]
    return []


async def get_channel_catalog(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/channels/catalog")
    if isinstance(resp, dict) and isinstance(resp.get("channels"), dict):
        return resp["channels"]
    return {}


async def create_channel(
    client: Client,
    *,
    channel_type: str,
    mode: str = "",
    auth_mode: str = "",
    response_mode: str = "",
    enabled: Optional[bool] = None,
    config: Optional[dict[str, Any]] = None,
) -> Channel:
    body: dict[str, Any] = {"channel_type": channel_type}
    if mode:
        body["mode"] = mode
    if auth_mode:
        body["auth_mode"] = auth_mode
    if response_mode:
        body["response_mode"] = response_mode
    if enabled is not None:
        body["enabled"] = enabled
    if config:
        body["config"] = config
    resp = await client.post_json("/api/channels", body)
    if isinstance(resp, dict) and isinstance(resp.get("channel"), dict):
        return Channel.from_dict(resp["channel"])
    raise RuntimeError("unexpected /api/channels response")


async def get_channel(client: Client, channel_id: str) -> Channel:
    resp = await client.get_json(f"/api/channels/{quote(channel_id, safe='')}")
    if isinstance(resp, dict) and isinstance(resp.get("channel"), dict):
        return Channel.from_dict(resp["channel"])
    raise RuntimeError("unexpected /api/channels/{id} response")


async def update_channel(
    client: Client, channel_id: str, fields: dict[str, Any],
) -> Channel:
    """PATCH a channel with the given fields (``mode`` / ``auth_mode`` /
    ``response_mode`` / ``enabled`` / ``config``).

    The server merges ``config`` into the existing config (so secrets you never
    received aren't clobbered), validates, and restarts the adapter when
    anything runtime-affecting changed. The ``main`` channel can't be modified.
    """
    resp = await client.patch_json(
        f"/api/channels/{quote(channel_id, safe='')}",
        fields,
    )
    if isinstance(resp, dict) and isinstance(resp.get("channel"), dict):
        return Channel.from_dict(resp["channel"])
    raise RuntimeError("unexpected /api/channels/{id} response")


async def set_notification_filter(
    client: Client, channel_id: str, notification_filter: dict[str, Any],
) -> Channel:
    """PATCH ``config.notification_filter`` on a notification-mode channel.

    Thin wrapper over :func:`update_channel`; the filter is validated/normalized
    server-side (HTTP 400 on invalid) and the adapter restarts so it takes
    effect immediately.
    """
    return await update_channel(
        client, channel_id, {"config": {"notification_filter": notification_filter}},
    )


async def list_senders(client: Client, channel_id: str) -> list[dict[str, Any]]:
    """List the senders seen on a channel (``pending_otp`` is redacted)."""
    resp = await client.get_json(f"/api/channels/{quote(channel_id, safe='')}/senders")
    if isinstance(resp, dict) and isinstance(resp.get("senders"), list):
        return [s for s in resp["senders"] if isinstance(s, dict)]
    return []


async def update_sender(
    client: Client, channel_id: str, sender_id: str, fields: dict[str, Any],
) -> dict[str, Any]:
    """PATCH one channel sender. ``fields`` may carry ``authenticated``/``phone``.

    The sender must already exist (they've contacted the channel, or a direct
    send reached them), else the server 404s — so a typo can't seed a junk row.
    """
    resp = await client.patch_json(
        f"/api/channels/{quote(channel_id, safe='')}/senders/{quote(sender_id, safe='')}",
        fields,
    )
    if isinstance(resp, dict) and isinstance(resp.get("sender"), dict):
        return resp["sender"]
    raise RuntimeError("unexpected /api/channels/{id}/senders/{sender} response")


async def set_sender_authenticated(
    client: Client, channel_id: str, sender_id: str, authenticated: bool,
) -> dict[str, Any]:
    """Approve (``authenticated=True``) or revoke a channel subscriber.

    Backs ``cremind channels approve/revoke`` — the operator side of the
    ``approval`` subscription-auth method for notification channels.
    """
    return await update_sender(
        client, channel_id, sender_id, {"authenticated": authenticated},
    )


async def set_sender_phone(
    client: Client, channel_id: str, sender_id: str, phone: str | None,
) -> dict[str, Any]:
    """Record (or clear, with ``None``) a contact's phone number.

    Backs ``cremind channels set-phone``. This is the deliberate-overwrite
    path: automatic derivation only ever fills an empty number, so correcting a
    mapping by hand has to go through here.
    """
    return await update_sender(client, channel_id, sender_id, {"phone": phone})


async def set_sender_confirmation(
    client: Client, channel_id: str, sender_id: str, mode: str | None,
) -> dict[str, Any]:
    """Set this client's confirmation override: ``"required"``, ``"skip"``, ``None``.

    ``None`` clears the override so the client inherits the profile's "confirm
    before messaging clients" setting. Backs ``cremind channels set-confirm``.
    """
    return await update_sender(
        client, channel_id, sender_id, {"send_confirmation": mode},
    )


async def send_channel_message(
    client: Client, channel_id: str, recipients: list[dict[str, Any]],
    message: str | None = None, dry_run: bool = True,
    default_country_code: str | None = None,
) -> dict[str, Any]:
    """Message specific clients on a channel — one or many.

    Unlike :func:`notify_channel` (which broadcasts to the channel's own
    subscribers), this addresses named individuals by platform sender id or
    phone number and records each delivered message in that client's
    conversation. ``dry_run`` defaults to True: it resolves the recipients and
    reports who *would* be messaged without sending anything.
    """
    payload: dict[str, Any] = {"recipients": recipients, "dry_run": dry_run}
    if message is not None:
        payload["message"] = message
    if default_country_code:
        payload["default_country_code"] = default_country_code
    resp = await client.post_json(
        f"/api/channels/{quote(channel_id, safe='')}/message", payload,
    )
    return resp if isinstance(resp, dict) else {}


async def clear_sender_history(
    client: Client, channel_id: str, sender_id: str,
) -> dict[str, Any]:
    """Wipe one subscriber's conversation history, keeping the conversation.

    Their next message continues in the same conversation, and the per-sender
    token/cost totals (attributed by conversation) survive. Returns
    ``{"conversation_id", "cleared_messages"}``.
    """
    resp = await client.delete(
        f"/api/channels/{quote(channel_id, safe='')}"
        f"/senders/{quote(sender_id, safe='')}/messages",
    )
    return resp if isinstance(resp, dict) else {}


async def delete_sender(
    client: Client, channel_id: str, sender_id: str,
) -> dict[str, Any]:
    """Delete a channel client outright — as if they had never messaged.

    The full-erasure counterpart of :func:`clear_sender_history`: removes their
    conversation and messages, the automations homed on it, their contact
    details and their access state, so a later message arrives as a first
    contact. Returns ``{"conversation_id", "deleted_messages", ...}``.
    """
    resp = await client.delete(
        f"/api/channels/{quote(channel_id, safe='')}"
        f"/senders/{quote(sender_id, safe='')}",
    )
    return resp if isinstance(resp, dict) else {}


async def delete_channel(client: Client, channel_id: str) -> None:
    await client.delete(f"/api/channels/{quote(channel_id, safe='')}")


async def notify_channel(
    client: Client, channel_id: str, message: str,
) -> dict[str, Any]:
    """Push an ad-hoc ``message`` OUT to a notification-mode channel.

    Delivers to the channel's recipients (configured target chat IDs ∪
    authenticated subscribers) via the running adapter, bypassing the channel's
    notification filter. Returns ``{"delivered": bool, "recipients": int}``.
    The server rejects non-notification channels (HTTP 400) and channels whose
    adapter isn't running (HTTP 409).
    """
    resp = await client.post_json(
        f"/api/channels/{quote(channel_id, safe='')}/notify",
        {"message": message},
    )
    return resp if isinstance(resp, dict) else {}


def channel_auth_events_path(channel_id: str) -> str:
    return f"/api/channels/{quote(channel_id, safe='')}/auth-events"


async def submit_channel_auth_input(
    client: Client,
    channel_id: str,
    code: str = "",
    password: str = "",
) -> None:
    body: dict[str, str] = {}
    if code:
        body["code"] = code
    if password:
        body["password"] = password
    await client.post_json(
        f"/api/channels/{quote(channel_id, safe='')}/auth-input",
        body,
    )


# ── channel group chats ───────────────────────────────────────────────────
#
# Platform groups this channel's account is in — a Telegram supergroup, a Slack
# channel — NOT ``cremind group``, which is Cremind's own multi-agent rooms.


async def list_channel_groups(
    client: Client, channel_id: str, status: str = "",
) -> list[dict[str, Any]]:
    """The platform groups this channel knows about, newest first."""
    path = f"/api/channels/{quote(channel_id, safe='')}/groups"
    if status:
        path = f"{path}?status={quote(status, safe='')}"
    resp = await client.get_json(path)
    if isinstance(resp, dict) and isinstance(resp.get("groups"), list):
        return [g for g in resp["groups"] if isinstance(g, dict)]
    return []


async def update_channel_group(
    client: Client, channel_id: str, group_id: str, fields: dict[str, Any],
) -> dict[str, Any]:
    """PATCH one group — ``status``, ``settings`` and/or ``title``."""
    resp = await client.patch_json(
        f"/api/channels/{quote(channel_id, safe='')}/groups/"
        f"{quote(group_id, safe='')}",
        fields,
    )
    if isinstance(resp, dict) and isinstance(resp.get("group"), dict):
        return resp["group"]
    raise RuntimeError("unexpected /api/channels/{id}/groups/{group} response")


async def set_channel_group_status(
    client: Client, channel_id: str, group_id: str, status: str,
) -> dict[str, Any]:
    return await update_channel_group(
        client, channel_id, group_id, {"status": status},
    )


async def set_channel_group_settings(
    client: Client, channel_id: str, group_id: str, settings: dict[str, Any],
) -> dict[str, Any]:
    """Patch a group's settings. Merged server-side one level deep."""
    return await update_channel_group(
        client, channel_id, group_id, {"settings": settings},
    )


async def delete_channel_group(
    client: Client, channel_id: str, group_id: str,
) -> None:
    """Forget a group outright, transcript included."""
    await client.delete(
        f"/api/channels/{quote(channel_id, safe='')}/groups/"
        f"{quote(group_id, safe='')}"
    )


async def list_available_channel_groups(
    client: Client, channel_id: str,
) -> dict[str, Any]:
    """Groups the account is already in. ``{"supported": bool, "groups": [...]}``.

    ``supported`` false means the platform will not enumerate them — a fact
    about the platform, not a failure.
    """
    resp = await client.get_json(
        f"/api/channels/{quote(channel_id, safe='')}/groups/available"
    )
    if not isinstance(resp, dict):
        return {"supported": False, "groups": []}
    return {
        "supported": bool(resp.get("supported")),
        "groups": [g for g in (resp.get("groups") or []) if isinstance(g, dict)],
    }


async def add_channel_group(
    client: Client,
    channel_id: str,
    platform_chat_id: str,
    *,
    title: str = "",
    chat_type: str = "",
) -> dict[str, Any]:
    """Enable a group the account is already in — approved on the spot.

    Picking IS approving: the caller is naming a specific group out of their own
    list, which is the decision the approve step exists to collect.
    """
    body: dict[str, Any] = {"platform_chat_id": platform_chat_id}
    if title:
        body["title"] = title
    if chat_type:
        body["chat_type"] = chat_type
    resp = await client.post_json(
        f"/api/channels/{quote(channel_id, safe='')}/groups", body,
    )
    if isinstance(resp, dict) and isinstance(resp.get("group"), dict):
        return resp["group"]
    raise RuntimeError("unexpected POST /api/channels/{id}/groups response")


async def refresh_channel_group_roster(
    client: Client, channel_id: str, group_id: str,
) -> dict[str, Any]:
    """Ask the platform who is in a group. ``{"group": …, "source": …}``."""
    resp = await client.post_json(
        f"/api/channels/{quote(channel_id, safe='')}/groups/"
        f"{quote(group_id, safe='')}/roster",
        {},
    )
    return resp if isinstance(resp, dict) else {}


async def resolve_channel_group(
    client: Client, channel_id: str, ref: str,
) -> dict[str, Any]:
    """Find one group by id, platform chat id, or unique title.

    A group id is a uuid nobody types, so the two things an operator actually
    has — the chat id they can see on the platform, and the title they can read
    — resolve too. An ambiguous title raises rather than guessing: picking one
    of two rooms called "Ops" and approving it is not a recoverable mistake.
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise RuntimeError("no group given")
    groups = await list_channel_groups(client, channel_id)
    for group in groups:
        if group.get("id") == wanted:
            return group
    for group in groups:
        if str(group.get("platform_chat_id") or "") == wanted:
            return group
    matches = [
        g for g in groups
        if str(g.get("title") or "").strip().lower() == wanted.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(str(g.get("id")) for g in matches)
        raise RuntimeError(
            f"{len(matches)} groups are called {wanted!r} on this channel; "
            f"use one of these ids: {ids}"
        )
    raise RuntimeError(
        f"no group on this channel matches {wanted!r} — list them with "
        f"`cremind channels groups list {channel_id}`"
    )
