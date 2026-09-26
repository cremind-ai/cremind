"""Identity introspection — `GET /api/me`.

Mirrors `cli/internal/client/tokens.go`. The server returns JWT-derived fields
plus working-directory metadata; this module exposes a typed `Me` view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.cli.client._base import Client


@dataclass(frozen=True)
class Me:
    subject: str
    profile: str
    issued_at: int
    expires_at: int
    system_dir: str
    #: This profile's own working directory (each profile has its own).
    user_working_dir: str
    #: ``/api/me``'s ``user_working_dir_default`` as sent — whether the folder
    #: is the profile's default (a bool), or the default's path (a string).
    #: ``None`` from a server that predates it.
    user_working_dir_default: Any = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Me":
        raw_default = d.get("user_working_dir_default")
        return cls(
            subject=str(d.get("sub") or ""),
            profile=str(d.get("profile") or ""),
            issued_at=int(d.get("iat") or 0),
            expires_at=int(d.get("exp") or 0),
            system_dir=str(d.get("system_dir") or ""),
            user_working_dir=str(d.get("user_working_dir") or ""),
            user_working_dir_default=raw_default if isinstance(raw_default, (bool, str)) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "subject": self.subject,
            "profile": self.profile,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "system_dir": self.system_dir,
            "user_working_dir": self.user_working_dir,
        }
        if self.user_working_dir_default is not None:
            out["user_working_dir_default"] = self.user_working_dir_default
        return out


async def get_me(client: Client) -> Me:
    data = await client.get_json("/api/me")
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /api/me response shape: {type(data).__name__}")
    return Me.from_dict(data)
