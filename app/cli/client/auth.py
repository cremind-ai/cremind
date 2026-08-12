"""Token status & rotation — `/api/auth/*`.

Two producers feed the same views: the REST endpoints wrapped here, and the
in-process helper behind ``cremind auth regenerate --local``. Both build these
dataclasses, so the rendered table and the ``--json`` keys can't drift between
the authenticated and recovery paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.cli.client._base import Client


@dataclass(frozen=True)
class AuthStatus:
    profile: str
    subject: str
    issued_at: int  # epoch seconds; 0 when unknown
    expires_at: int  # epoch seconds; 0 when unknown
    token_serial: int  # serial carried by the presented token
    current_serial: int  # serial stored on the profile row
    valid: bool
    token_file: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthStatus":
        return cls(
            profile=str(d.get("profile") or ""),
            subject=str(d.get("sub") or ""),
            issued_at=int(d.get("iat") or 0),
            expires_at=int(d.get("exp") or 0),
            token_serial=int(d.get("token_serial") or 0),
            current_serial=int(d.get("current_serial") or 0),
            valid=bool(d.get("valid")),
            token_file=str(d.get("token_file") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "subject": self.subject,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "token_serial": self.token_serial,
            "current_serial": self.current_serial,
            "valid": self.valid,
            "token_file": self.token_file,
        }


@dataclass(frozen=True)
class RotatedToken:
    profile: str
    token: str
    expires_at: str  # ISO-8601 — what BaseConfig.mint_token returns
    serial: int
    token_file: str  # the path the SERVER wrote; "" if it didn't

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RotatedToken":
        return cls(
            profile=str(d.get("profile") or ""),
            token=str(d.get("token") or ""),
            expires_at=str(d.get("expires_at") or ""),
            serial=int(d.get("serial") or 0),
            token_file=str(d.get("token_file") or ""),
        )

    def to_dict(self, *, include_token: bool) -> dict[str, Any]:
        """Serialise for ``--json``.

        The token is omitted unless explicitly asked for — printing a live
        credential by default would land it in shell scrollback and, because
        agents run ``cremind`` through ``exec_shell``, in conversation history.
        """
        out: dict[str, Any] = {
            "profile": self.profile,
            "expires_at": self.expires_at,
            "serial": self.serial,
            "token_file": self.token_file,
        }
        if include_token:
            out["token"] = self.token
        return out


async def get_auth_status(client: Client, *, profile: Optional[str] = None) -> AuthStatus:
    """GET /api/auth/status — auth required."""
    path = "/api/auth/status"
    if profile:
        from urllib.parse import quote

        path = f"{path}?profile={quote(profile, safe='')}"
    data = await client.get_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /api/auth/status response shape: {type(data).__name__}")
    return AuthStatus.from_dict(data)


async def regenerate_token(
    client: Client,
    *,
    profile: Optional[str] = None,
    expires_hours: Optional[int] = None,
) -> RotatedToken:
    """POST /api/auth/regenerate — auth required; admin to target another profile."""
    body: dict[str, Any] = {}
    if profile:
        body["profile"] = profile
    if expires_hours is not None:
        body["expires_hours"] = expires_hours
    data = await client.post_json("/api/auth/regenerate", body)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /api/auth/regenerate response shape: {type(data).__name__}")
    return RotatedToken.from_dict(data)
