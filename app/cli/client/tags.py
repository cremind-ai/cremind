"""Cremind Tag endpoints — `/api/tags/*` (profile) and `/api/tags/hardware/*` (admin).

Thin async wrappers: each returns the server's JSON object as-is (an empty
dict when the body is not an object), so `cremind --json tags …` prints
exactly what the server said. Timestamps in those objects are epoch
**milliseconds**. Errors surface as `APIError`; its `raw` carries the
`{"error", "message", "detail"}` body the commands read their hints from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


def _seg(value: Any) -> str:
    """One path segment, escaped (ids come from the user's command line)."""
    return quote(str(value), safe="")


def _obj(resp: Any) -> dict[str, Any]:
    return resp if isinstance(resp, dict) else {}


# ── the profile's own tags ─────────────────────────────────────────────────


async def overview(client) -> dict[str, Any]:
    """`{profile, enabled, devices, counts}` — the tags this profile owns."""
    return _obj(await client.get_json("/api/tags"))


async def get_settings(client) -> dict[str, Any]:
    return _obj(await client.get_json("/api/tags/settings"))


async def put_settings(client, body: dict[str, Any]) -> dict[str, Any]:
    """`{enabled?, options?}` — `options` REPLACES the profile's own overrides."""
    return _obj(await client.put_json("/api/tags/settings", body))


async def patch_settings(client, body: dict[str, Any]) -> dict[str, Any]:
    """`{enabled?, options?}` — `options` MERGES: keys given replace, `null`
    drops the profile's override (inherit again), `routes` merges per kind."""
    return _obj(await client.patch_json("/api/tags/settings", body))


async def get_device(client, device_id: str) -> dict[str, Any]:
    """`{device, deliveries}` (the 20 latest deliveries)."""
    return _obj(await client.get_json(f"/api/tags/devices/{_seg(device_id)}"))


async def rename_device(client, device_id: str, name: str) -> dict[str, Any]:
    return _obj(await client.patch_json(f"/api/tags/devices/{_seg(device_id)}", {"name": name}))


async def display(client, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Pin a note: `{title, body?, icon?, ttl_s?}` -> `{delivery}`."""
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/display", body))


async def clear(client, device_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/clear", {}))


async def refresh(client, device_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/refresh", {}))


async def identify(client, device_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/identify", {}))


async def get_preview(client, device_id: str, *, kind: str = "displayed") -> tuple[bytes, Optional[int]]:
    """The preview PNG and its screen revision (`X-Tag-Revision`)."""
    png, headers = await client.get_bytes(
        f"/api/tags/devices/{_seg(device_id)}/preview", params={"kind": kind},
    )
    try:
        revision: Optional[int] = int(headers.get("x-tag-revision", ""))
    except ValueError:
        revision = None
    return png, revision


async def download_preview(client, device_id: str, path: str | Path, *,
                           kind: str = "displayed") -> dict[str, Any]:
    """Save the preview PNG to `path`. The file is written only once the
    server has answered 200, so a missing preview leaves nothing behind."""
    png, revision = await get_preview(client, device_id, kind=kind)
    target = Path(path)
    target.write_bytes(png)
    return {"path": str(target), "kind": kind, "revision": revision, "bytes": len(png)}


async def list_deliveries(
    client,
    *,
    device: Optional[str] = None,
    state: Optional[str] = None,
    limit: Optional[int] = None,
    before: Optional[int] = None,
) -> dict[str, Any]:
    """`{deliveries, next_before}`, newest first."""
    params: dict[str, Any] = {}
    if device:
        params["device"] = device
    if state:
        params["state"] = state
    if limit is not None:
        params["limit"] = limit
    if before is not None:
        params["before"] = before
    return _obj(await client.get_json("/api/tags/deliveries", params=params or None))


async def get_delivery(client, delivery_id: int | str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/deliveries/{_seg(delivery_id)}"))


async def cancel_delivery(client, delivery_id: int | str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/deliveries/{_seg(delivery_id)}/cancel", {}))


async def list_companions(client) -> dict[str, Any]:
    """`{companions: [{id, name, online, last_seen_at, version}]}`."""
    return _obj(await client.get_json("/api/tags/companions"))


async def list_credentials(client) -> dict[str, Any]:
    """`{credentials}` — this profile's content credentials (never secrets)."""
    return _obj(await client.get_json("/api/tags/credentials"))


async def create_credential(client, companion_id: str, label: Optional[str] = None) -> dict[str, Any]:
    """`{credential, secret, authorization}` — the only time the secret is sent."""
    body: dict[str, Any] = {"companion_id": companion_id}
    if label:
        body["label"] = label
    return _obj(await client.post_json("/api/tags/credentials", body))


async def revoke_credential(client, credential_id: str) -> dict[str, Any]:
    return _obj(await client.delete(f"/api/tags/credentials/{_seg(credential_id)}"))


# ── hardware (admin profile) ────────────────────────────────────────────────


async def hardware_inventory(client) -> dict[str, Any]:
    """`{companions, devices, commands}` — every companion, device and recent command."""
    return _obj(await client.get_json("/api/tags/hardware"))


async def register_companion(client, name: str) -> dict[str, Any]:
    """`{companion, credential, secret, authorization}`."""
    return _obj(await client.post_json("/api/tags/hardware/companions", {"name": name}))


async def rotate_companion(client, companion_id: str) -> dict[str, Any]:
    """`{credential, secret, authorization, revoked}`."""
    return _obj(await client.post_json(f"/api/tags/hardware/companions/{_seg(companion_id)}/rotate", {}))


async def delete_companion(client, companion_id: str) -> dict[str, Any]:
    return _obj(await client.delete(f"/api/tags/hardware/companions/{_seg(companion_id)}"))


async def create_command(client, companion_id: str, kind: str,
                         args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Queue a hardware operation -> `{command}`."""
    body: dict[str, Any] = {"companion_id": companion_id, "kind": kind}
    if args:
        body["args"] = args
    return _obj(await client.post_json("/api/tags/hardware/commands", body))


async def get_command(client, command_id: str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/hardware/commands/{_seg(command_id)}"))


async def claim_tag(client, device_id: str, owner: str, *, bridge_id: Optional[str] = None,
                    name: Optional[str] = None) -> dict[str, Any]:
    """`{device, commands}` — give the tag to `owner` (a profile name)."""
    body: dict[str, Any] = {"owner": owner}
    if bridge_id:
        body["bridge_id"] = bridge_id
    if name is not None:
        body["name"] = name
    return _obj(await client.post_json(f"/api/tags/hardware/tags/{_seg(device_id)}/claim", body))


async def assign_tag(client, device_id: str, bridge_id: str) -> dict[str, Any]:
    """`{device, command}` — move the tag to another bridge."""
    return _obj(await client.post_json(f"/api/tags/hardware/tags/{_seg(device_id)}/assign",
                                       {"bridge_id": bridge_id}))


async def release_tag(client, device_id: str) -> dict[str, Any]:
    """`{device, commands}` — the tag belongs to nobody and is blanked."""
    return _obj(await client.post_json(f"/api/tags/hardware/tags/{_seg(device_id)}/release", {}))


async def rename_hardware_device(client, device_id: str, name: str) -> dict[str, Any]:
    return _obj(await client.patch_json(f"/api/tags/hardware/devices/{_seg(device_id)}", {"name": name}))


async def delete_hardware_device(client, device_id: str) -> dict[str, Any]:
    return _obj(await client.delete(f"/api/tags/hardware/devices/{_seg(device_id)}"))


async def get_defaults(client) -> dict[str, Any]:
    """`{defaults, builtin}` — what every profile inherits."""
    return _obj(await client.get_json("/api/tags/hardware/defaults"))


async def put_defaults(client, defaults: dict[str, Any]) -> dict[str, Any]:
    """Replace the admin defaults (the whole object) -> `{defaults, builtin}`."""
    return _obj(await client.put_json("/api/tags/hardware/defaults", {"defaults": defaults}))


async def patch_defaults(client, defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge into the admin defaults (`null` drops one, `routes` merges per
    kind) -> `{defaults, builtin}`."""
    return _obj(await client.patch_json("/api/tags/hardware/defaults", {"defaults": defaults}))
