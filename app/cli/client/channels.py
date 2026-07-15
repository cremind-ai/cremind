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


async def set_sender_authenticated(
    client: Client, channel_id: str, sender_id: str, authenticated: bool,
) -> dict[str, Any]:
    """Approve (``authenticated=True``) or revoke a channel subscriber.

    Backs ``cremind channels approve/revoke`` — the operator side of the
    ``approval`` subscription-auth method for notification channels. The sender
    must already exist (they've contacted the channel), else the server 404s.
    """
    resp = await client.patch_json(
        f"/api/channels/{quote(channel_id, safe='')}/senders/{quote(sender_id, safe='')}",
        {"authenticated": authenticated},
    )
    if isinstance(resp, dict) and isinstance(resp.get("sender"), dict):
        return resp["sender"]
    raise RuntimeError("unexpected /api/channels/{id}/senders/{sender} response")


async def delete_channel(client: Client, channel_id: str) -> None:
    await client.delete(f"/api/channels/{quote(channel_id, safe='')}")


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
