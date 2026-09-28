"""Simple device setup endpoints — `/api/tags/connections`, setup sessions,
discovery, pairings, device actions and recoveries (cremind-tag
docs/setup-api.md §1).

Thin async wrappers returning the server's JSON object as-is. Every mutation
carries an ``idempotency_key`` so a retried command never does the work twice.
Setup codes travel in the request body only; nothing here logs them.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional
from urllib.parse import quote


def _seg(value: Any) -> str:
    return quote(str(value), safe="")


def _obj(resp: Any) -> dict[str, Any]:
    return resp if isinstance(resp, dict) else {}


def _key() -> str:
    return str(uuid.uuid4())


async def connections(client) -> dict[str, Any]:
    """`{simple_setup, connections, computers, active: {sessions, operations}}`."""
    return _obj(await client.get_json("/api/tags/connections"))


async def installers(client) -> dict[str, Any]:
    return _obj(await client.get_json("/api/tags/connect"))


async def create_session(client, operation: str, server_url: str, companion_id: Optional[str] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"operation": operation, "server_url": server_url, "idempotency_key": _key()}
    if companion_id:
        body["companion_id"] = companion_id
    return _obj(await client.post_json("/api/tags/setup-sessions", body))


async def get_session(client, session_id: str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/setup-sessions/{_seg(session_id)}"))


async def confirm_session(client, session_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/setup-sessions/{_seg(session_id)}/confirm",
                                       {"idempotency_key": _key()}))


async def cancel_session(client, session_id: str) -> dict[str, Any]:
    return _obj(await client.delete(f"/api/tags/setup-sessions/{_seg(session_id)}"))


async def start_discovery(client, role: str, setup_code: str, *, gateway_id: Optional[str] = None,
                          duration_s: Optional[int] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"role": role, "setup_code": setup_code, "idempotency_key": _key()}
    if gateway_id:
        body["gateway_id"] = gateway_id
    if duration_s:
        body["duration_s"] = duration_s
    return _obj(await client.post_json("/api/tags/discovery", body))


async def get_discovery(client, discovery_id: str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/discovery/{_seg(discovery_id)}"))


async def start_pairing(client, discovery_id: str, candidate_id: str, name: Optional[str] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"discovery_id": discovery_id, "candidate_id": candidate_id, "idempotency_key": _key()}
    if name:
        body["name"] = name
    return _obj(await client.post_json("/api/tags/pairings", body))


async def get_pairing(client, pairing_id: str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/pairings/{_seg(pairing_id)}"))


async def cancel_pairing(client, pairing_id: str) -> dict[str, Any]:
    return _obj(await client.delete(f"/api/tags/pairings/{_seg(pairing_id)}"))


async def unpair(client, device_id: str, *, force: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"idempotency_key": _key()}
    if force:
        body["force"] = True
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/unpair", body))


async def pause(client, device_id: str, paused: bool) -> dict[str, Any]:
    verb = "pause" if paused else "resume"
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/{verb}", {"idempotency_key": _key()}))


async def move(client, device_id: str, bridge_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/move",
                                       {"bridge_id": bridge_id, "idempotency_key": _key()}))


async def test_card(client, device_id: str) -> dict[str, Any]:
    return _obj(await client.post_json(f"/api/tags/devices/{_seg(device_id)}/test", {"idempotency_key": _key()}))


async def start_recovery(client, companion_id: str, server_url: str) -> dict[str, Any]:
    return _obj(await client.post_json("/api/tags/recoveries", {
        "companion_id": companion_id, "server_url": server_url, "idempotency_key": _key()}))


async def get_recovery(client, recovery_id: str) -> dict[str, Any]:
    return _obj(await client.get_json(f"/api/tags/recoveries/{_seg(recovery_id)}"))
