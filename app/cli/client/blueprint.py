"""Client wrappers for the Blueprint REST endpoints.

Thin async functions over :class:`app.cli.client._base.Client`. No ``app.*``
server imports — the offline ``inspect`` path in the command module reads the
archive's manifest via the engine directly instead of going through here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.cli.client._base import Client

# Cremind Hub base URL — the marketplace the publish/install flows talk to. Talks to a
# DIFFERENT host than the local server (`Client`), so these use plain httpx. Override with
# CREMIND_HUB_URL (e.g. http://localhost:8788) for local development.
_HUB_DEFAULT_URL = "https://hub.cremind.io"


def hub_base() -> str:
    return os.environ.get("CREMIND_HUB_URL", _HUB_DEFAULT_URL).rstrip("/")


async def get_exportable(client: Client) -> Any:
    return await client.get_json("/api/blueprints/exportable")


async def export_blueprint(client: Client, body: dict[str, Any]) -> Any:
    return await client.post_json("/api/blueprints/export", body)


async def list_blueprints(client: Client) -> Any:
    return await client.get_json("/api/blueprints")


async def download(client: Client, name: str, sink: Any) -> None:
    await client.download(f"/api/blueprints/download/{name}", sink)


async def delete(client: Client, name: str) -> Any:
    return await client.delete(f"/api/blueprints/{name}")


async def upload(client: Client, path: str, *, replace: bool = False) -> Any:
    with open(path, "rb") as f:
        data = f.read()
    params = {"replace": "true"} if replace else None
    # Low-level post so the ``?replace`` query param rides along with multipart.
    resp = await client._http.post(  # noqa: SLF001 — intentional low-level use
        "/api/blueprints/import/upload",
        params=params,
        files=[("file", (os.path.basename(path), data))],
    )
    client._check_response(resp)  # noqa: SLF001
    return resp.json() if resp.content else None


async def get_session(client: Client) -> Any:
    return await client.get_json("/api/blueprints/import/session")


async def apply_step(client: Client, key: str, body: dict[str, Any] | None) -> Any:
    return await client.post_json(f"/api/blueprints/import/steps/{key}", body or {})


async def skip_step(client: Client, key: str) -> Any:
    return await client.post_json(f"/api/blueprints/import/steps/{key}/skip", {})


async def finalize(client: Client) -> Any:
    return await client.post_json("/api/blueprints/import/finalize", {})


async def abort(client: Client, *, delete_profile: bool = True) -> Any:
    return await client.post_json(
        "/api/blueprints/import/abort", {"delete_profile": delete_profile}
    )


async def import_hub(client: Client, link: str, *, replace: bool = False) -> Any:
    """Stage a hub-downloaded blueprint into the wizard (server-side download)."""
    params = {"replace": "true"} if replace else None
    resp = await client._http.post(  # noqa: SLF001 — carry the ?replace query param
        "/api/blueprints/import/hub",
        params=params,
        json={"link": link},
    )
    client._check_response(resp)  # noqa: SLF001
    return resp.json() if resp.content else None


# ── Cremind Hub publish (device-code flow + upload) ────────────────────────────
# These talk to the HUB, not the local server. The local JWT is never sent to the hub;
# the hub publish token (from the device flow) is the only credential used for the upload.


@dataclass(frozen=True)
class PublishDeviceStart:
    verification_uri: str
    verification_uri_complete: str
    user_code: str
    device_code: str
    expires_in: int
    interval: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PublishDeviceStart":
        return cls(
            verification_uri=str(d.get("verification_uri") or ""),
            verification_uri_complete=str(d.get("verification_uri_complete") or ""),
            user_code=str(d.get("user_code") or ""),
            device_code=str(d.get("device_code") or ""),
            expires_in=int(d.get("expires_in") or 0),
            interval=int(d.get("interval") or 5),
        )


@dataclass(frozen=True)
class PublishDevicePoll:
    status: str  # pending | complete | expired | denied
    publish_token: str
    error: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PublishDevicePoll":
        return cls(
            status=str(d.get("status") or ""),
            publish_token=str(d.get("publish_token") or ""),
            error=str(d.get("error") or ""),
        )


async def publish_device_start(name: str, display: str) -> PublishDeviceStart:
    async with httpx.AsyncClient(base_url=hub_base(), timeout=30) as http:
        resp = await http.post(
            "/api/publish/device/start", json={"app": "cremind", "name": name, "display": display}
        )
        resp.raise_for_status()
        return PublishDeviceStart.from_dict(resp.json())


async def publish_device_poll(device_code: str) -> PublishDevicePoll:
    async with httpx.AsyncClient(base_url=hub_base(), timeout=30) as http:
        resp = await http.post("/api/publish/device/token", json={"device_code": device_code})
        resp.raise_for_status()
        return PublishDevicePoll.from_dict(resp.json())


async def upload_to_hub(token: str, filename: str, data: bytes) -> dict[str, Any]:
    """Upload a `.cremind-blueprint` to the hub with a Bearer publish token."""
    async with httpx.AsyncClient(base_url=hub_base(), timeout=120) as http:
        resp = await http.post(
            "/api/blueprints",
            headers={"Authorization": f"Bearer {token}"},
            files=[("file", (filename, data, "application/gzip"))],
        )
        if resp.status_code >= 400:
            msg = resp.text
            try:
                body = resp.json()
                msg = body.get("message") or body.get("error") or msg
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Hub upload failed ({resp.status_code}): {msg}")
        return resp.json() if resp.content else {}
