"""Setup endpoints — `/api/config/setup*`, `/api/config/server`, `/api/config/reconfigure`.

Mirrors `cli/internal/client/setup.go`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.cli.client._base import Client


def _dict_tuple(value: Any) -> tuple[dict, ...]:
    return tuple(v for v in value if isinstance(v, dict)) if isinstance(value, list) else ()


def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


@dataclass(frozen=True)
class SetupResponse:
    success: bool
    token: str
    expires_at: str
    profile: str
    # Non-fatal problems with a setup that still succeeded, each
    # ``{"code": ..., "message": ...}``. The wizard shows these in its live
    # log; headless callers would otherwise never learn that, say, the
    # profile they just created has no model and cannot answer anything.
    warnings: tuple[dict[str, str], ...] = ()
    # The rest of what the endpoint has always returned. Every field is
    # defaulted and parsed defensively, so an older server that omits them —
    # or a newer one that adds more — still yields a usable response rather
    # than a KeyError three frames down.
    embedding_enabled: bool = False
    channels: tuple[dict, ...] = ()
    channel_errors: tuple[dict, ...] = ()
    #: A feature was pip-installed that the running process cannot import yet.
    restart_required: bool = False
    installed_features: tuple[str, ...] = ()
    failed_features: tuple[str, ...] = ()
    # HTTPS hand-off (CREMIND_SSL=after-setup). Separate from
    # ``restart_required`` on purpose: that one is about features.
    tls_pending: bool = False
    next_origin: str = ""
    restart_supported: bool = False
    tls_management: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SetupResponse":
        return cls(
            success=bool(d.get("success") or False),
            token=str(d.get("token") or ""),
            expires_at=str(d.get("expires_at") or ""),
            profile=str(d.get("profile") or ""),
            warnings=_dict_tuple(d.get("warnings")),
            embedding_enabled=bool(d.get("embedding_enabled") or False),
            channels=_dict_tuple(d.get("channels")),
            channel_errors=_dict_tuple(d.get("channel_errors")),
            restart_required=bool(d.get("restart_required") or False),
            installed_features=_str_tuple(d.get("installed_features")),
            failed_features=_str_tuple(d.get("failed_features")),
            tls_pending=bool(d.get("tls_pending") or False),
            next_origin=str(d.get("next_origin") or ""),
            restart_supported=bool(d.get("restart_supported") or False),
            tls_management=str(d.get("tls_management") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """The whole response, for ``--json`` output."""
        return {
            "success": self.success,
            "token": self.token,
            "expires_at": self.expires_at,
            "profile": self.profile,
            "warnings": [dict(w) for w in self.warnings],
            "embedding_enabled": self.embedding_enabled,
            "channels": [dict(c) for c in self.channels],
            "channel_errors": [dict(c) for c in self.channel_errors],
            "restart_required": self.restart_required,
            "installed_features": list(self.installed_features),
            "failed_features": list(self.failed_features),
            "tls_pending": self.tls_pending,
            "next_origin": self.next_origin,
            "restart_supported": self.restart_supported,
            "tls_management": self.tls_management,
        }


async def get_setup_status(
    client: Client,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """GET /api/config/setup-status — returns
    `{setup_complete, profile_exists?, has_profiles?}`. Unauthenticated.
    """
    params = {"profile": profile} if profile else None
    out = await client.get_json("/api/config/setup-status", params=params)
    return out if isinstance(out, dict) else {}


async def complete_setup(
    client: Client,
    body: dict[str, Any],
) -> SetupResponse:
    """POST /api/config/setup — unauthenticated. Returns the JWT token."""
    data = await client.post_json("/api/config/setup", body)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /api/config/setup response: {type(data).__name__}")
    return SetupResponse.from_dict(data)


async def reset_orphaned_setup(client: Client) -> None:
    """POST /api/config/reset-orphaned-setup — unauthenticated."""
    await client.post_json("/api/config/reset-orphaned-setup")


async def reconfigure(client: Client) -> None:
    """POST /api/config/reconfigure — admin auth required."""
    await client.post_json("/api/config/reconfigure")


async def get_server_config(client: Client) -> dict[str, Any]:
    """GET /api/config/server — returns `{config: {...}}`; this returns the
    inner `config` dict.
    """
    resp = await client.get_json("/api/config/server")
    if isinstance(resp, dict) and isinstance(resp.get("config"), dict):
        return resp["config"]
    return {}


async def update_server_config(
    client: Client,
    values: dict[str, Any],
) -> None:
    """PUT /api/config/server — partial write. The special key `jwt_secret`
    is stored as a secret server-side.
    """
    await client.put_json("/api/config/server", {"config": values})
