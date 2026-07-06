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
