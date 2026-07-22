"""LLM provider + model-group endpoints — `/api/llm/*`.

Mirrors `cli/internal/client/llm.go`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


# ── providers ─────────────────────────────────────────────────────────────

async def list_llm_providers(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/llm/providers")
    if isinstance(resp, dict) and isinstance(resp.get("providers"), list):
        return [p for p in resp["providers"] if isinstance(p, dict)]
    return []


async def get_provider_models(
    client: Client,
    provider: str,
) -> dict[str, Any]:
    out = await client.get_json(f"/api/llm/providers/{quote(provider, safe='')}/models")
    return out if isinstance(out, dict) else {}


async def configure_provider(
    client: Client,
    provider: str,
    kv: dict[str, Any],
) -> None:
    await client.put_json(f"/api/llm/providers/{quote(provider, safe='')}", kv)


async def create_custom_provider(
    client: Client,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Create a user-defined OpenAI-compatible custom provider.

    ``body`` = ``{display_name, base_url, api_key?, models: [...]}``.
    Returns the server response, including the internal ``name`` (``custom:<slug>``).
    """
    out = await client.post_json("/api/llm/providers/custom", body)
    return out if isinstance(out, dict) else {}


async def delete_provider_config(client: Client, provider: str) -> None:
    await client.delete(f"/api/llm/providers/{quote(provider, safe='')}/config")


# ── model groups ──────────────────────────────────────────────────────────

async def get_model_groups(client: Client) -> dict[str, Any]:
    out = await client.get_json("/api/llm/model-groups")
    return out if isinstance(out, dict) else {}


async def update_model_groups(
    client: Client,
    body: dict[str, Any],
) -> None:
    await client.put_json("/api/llm/model-groups", body)


# ── device-code flow ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceCodeStart:
    verification_uri: str
    user_code: str
    device_code: str
    expires_in: int
    interval: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceCodeStart":
        return cls(
            verification_uri=str(d.get("verification_uri") or ""),
            user_code=str(d.get("user_code") or ""),
            device_code=str(d.get("device_code") or ""),
            expires_in=int(d.get("expires_in") or 0),
            interval=int(d.get("interval") or 5),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_uri": self.verification_uri,
            "user_code": self.user_code,
            "device_code": self.device_code,
            "expires_in": self.expires_in,
            "interval": self.interval,
        }


@dataclass(frozen=True)
class DeviceCodePoll:
    status: str
    slow_down: bool
    access_token: str
    error: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceCodePoll":
        return cls(
            status=str(d.get("status") or ""),
            slow_down=bool(d.get("slow_down") or False),
            access_token=str(d.get("access_token") or ""),
            error=str(d.get("error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "slow_down": self.slow_down,
            "access_token": self.access_token,
            "error": self.error,
        }


async def device_code_start(client: Client) -> DeviceCodeStart:
    data = await client.post_json("/api/llm/auth/device-code/start")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected device-code/start response")
    return DeviceCodeStart.from_dict(data)


async def device_code_poll(client: Client, device_code: str) -> DeviceCodePoll:
    data = await client.post_json(
        "/api/llm/auth/device-code/poll",
        {"device_code": device_code},
    )
    if not isinstance(data, dict):
        raise RuntimeError("unexpected device-code/poll response")
    return DeviceCodePoll.from_dict(data)


# ── Codex OAuth flow ("Sign in with ChatGPT" for the OpenAI provider) ───────

@dataclass(frozen=True)
class CodexOAuthStart:
    authorize_url: str
    state: str
    redirect_uri: str
    listener_active: bool
    listener_error: str
    expires_in: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CodexOAuthStart":
        return cls(
            authorize_url=str(d.get("authorize_url") or ""),
            state=str(d.get("state") or ""),
            redirect_uri=str(d.get("redirect_uri") or ""),
            listener_active=bool(d.get("listener_active") or False),
            listener_error=str(d.get("listener_error") or ""),
            expires_in=int(d.get("expires_in") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorize_url": self.authorize_url,
            "state": self.state,
            "redirect_uri": self.redirect_uri,
            "listener_active": self.listener_active,
            "listener_error": self.listener_error,
            "expires_in": self.expires_in,
        }


@dataclass(frozen=True)
class CodexOAuthStatus:
    status: str
    email: str
    plan_type: str
    account_id: str
    error: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CodexOAuthStatus":
        return cls(
            status=str(d.get("status") or ""),
            email=str(d.get("email") or ""),
            plan_type=str(d.get("plan_type") or ""),
            account_id=str(d.get("account_id") or ""),
            error=str(d.get("error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "email": self.email,
            "plan_type": self.plan_type,
            "account_id": self.account_id,
            "error": self.error,
        }


async def codex_oauth_start(client: Client) -> CodexOAuthStart:
    data = await client.post_json("/api/llm/auth/codex/start")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected codex/start response")
    return CodexOAuthStart.from_dict(data)


async def codex_oauth_status(client: Client, state: str) -> CodexOAuthStatus:
    data = await client.get_json(f"/api/llm/auth/codex/status?state={quote(state, safe='')}")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected codex/status response")
    return CodexOAuthStatus.from_dict(data)


async def codex_oauth_complete(client: Client, redirect_url: str, state: str | None = None) -> CodexOAuthStatus:
    body: dict[str, Any] = {"redirect_url": redirect_url}
    if state:
        body["state"] = state
    data = await client.post_json("/api/llm/auth/codex/complete", body)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected codex/complete response")
    return CodexOAuthStatus.from_dict(data)
