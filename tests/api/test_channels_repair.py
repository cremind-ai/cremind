"""``POST /api/channels/{id}/repair`` — recover a channel whose pairing is stuck.

A saved pairing session that the platform invalidated behind our back — the same
account paired in another environment, a device revoked — is indistinguishable
on disk from a good one. Every session-based adapter prefers restoring it, so it
never enters the pairing flow again: no QR, no code, and a dialog that waits
forever. Deleting and re-adding the channel used to be the only way out, and it
took the channel's contacts and bound groups with it.

These pin the endpoint's contract: it resets only pairing channels, it does the
work in the order that keeps a half-finished repair safe, and it re-enables a
channel that a remote logout had disabled.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import app.api.channels as channels_api
from app.api.channels import get_channel_routes

_REPAIR = "/api/channels/{channel_id}/repair"

# Enough of the catalogue for ``_mode_setup_kind``: whatsapp/userbot pairs by
# QR, telegram/bot authenticates from a configured token.
_CATALOG = {
    "whatsapp": {
        "channel": {
            "display_name": "WhatsApp",
            "modes": [{"id": "userbot", "setup_kind": "qr"}],
        },
    },
    "telegram": {
        "channel": {
            "display_name": "Telegram",
            "modes": [
                {"id": "bot", "fields": {"bot_token": {"secret": True}}},
                {"id": "userbot", "setup_kind": "code"},
            ],
        },
    },
}


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", channel_id="ch1"):
    async def _json():
        return {}

    return SimpleNamespace(
        method="POST",
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params={"channel_id": channel_id},
        json=_json,
    )


class _Store:
    def __init__(self, **overrides):
        self.row = {
            "id": "ch1", "profile": "p1", "channel_type": "whatsapp",
            "mode": "userbot", "enabled": True, "config": {}, "state": {},
            **overrides,
        }
        self.updates: list[dict] = []

    async def get_channel(self, cid):
        return dict(self.row) if cid == self.row["id"] else None

    async def update_channel(self, cid, **fields):
        self.updates.append(dict(fields))
        self.row.update(fields)
        return dict(self.row)


class _SpyAdapter:
    """Stands in for a live adapter; records the reset without touching disk."""

    def __init__(self, channel=None, storage=None) -> None:
        self.channel = channel
        self.resets = 0

    def reset_session(self) -> None:
        self.resets += 1


class _Registry:
    def __init__(self, adapter=None, *, start_disables=False):
        self.adapter = adapter
        self.calls: list[str] = []
        self.start_kwargs: dict = {}
        self.start_disables = start_disables

    def get_adapter(self, _cid):
        return self.adapter

    async def stop_for_channel(self, cid):
        self.calls.append(f"stop:{cid}")

    async def start_for_channel(self, channel, *, install_if_missing=False):
        self.calls.append(f"start:{channel['id']}")
        self.start_kwargs = {"install_if_missing": install_if_missing}
        if self.start_disables:
            return {
                **channel, "enabled": False,
                "state": {**(channel.get("state") or {}), "last_error": "node missing"},
            }
        return dict(channel)

    def status_for(self, _cid):
        return "running"


def _patch(monkeypatch, registry, spy_adapter=None):
    monkeypatch.setattr(channels_api, "get_channel_registry", lambda: registry)
    monkeypatch.setattr(channels_api, "load_all_channel_catalogs", lambda: _CATALOG)
    if spy_adapter is not None:
        monkeypatch.setattr(
            "app.channels.registry.adapter_class_for_channel_type",
            lambda ct, mode: (lambda ch, st: spy_adapter),
        )


def _run(store, **req_kwargs):
    handler = _handler(store, _REPAIR, "POST")
    return asyncio.run(handler(_req(**req_kwargs)))


def test_repair_resets_the_session_then_restarts_the_adapter(monkeypatch):
    store = _Store()
    adapter = _SpyAdapter()
    registry = _Registry(adapter)
    _patch(monkeypatch, registry)

    resp = _run(store)

    assert resp.status_code == 200
    assert adapter.resets == 1
    # Order is the contract: stop first so the sidecar releases its session
    # files, start last so the fresh adapter finds nothing to restore.
    assert registry.calls == ["stop:ch1", "start:ch1"]
    # The restart may need the platform SDK that a failed boot never installed.
    assert registry.start_kwargs == {"install_if_missing": True}


def test_repair_refuses_a_channel_that_pairs_by_nothing(monkeypatch):
    """A bot-token channel has no session — resetting one would be a no-op
    dressed up as a fix, and would needlessly bounce a working adapter."""
    store = _Store(channel_type="telegram", mode="bot")
    adapter = _SpyAdapter()
    registry = _Registry(adapter)
    _patch(monkeypatch, registry)

    resp = _run(store)

    assert resp.status_code == 400
    assert "no pairing session" in json.loads(resp.body)["error"]
    assert adapter.resets == 0
    assert registry.calls == []


def test_repair_reenables_a_channel_a_remote_logout_disabled(monkeypatch):
    """``_mark_unlinked`` flips the row off, so repair must turn it back on.

    Without this the reset would land and ``start_for_channel`` would refuse a
    disabled row, leaving the user exactly as stuck as before.
    """
    store = _Store(
        enabled=False,
        state={
            "link_status": "unlinked",
            "unlinked_at": 1.0,
            "unlinked_reason": "logged_out_remote",
            "last_error": "logged out from your phone",
            "self_identity": {"user_id": "42"},
        },
    )
    registry = _Registry(_SpyAdapter())
    _patch(monkeypatch, registry)

    resp = _run(store)

    assert resp.status_code == 200
    assert store.row["enabled"] is True
    state = store.row["state"]
    for gone in ("link_status", "unlinked_at", "unlinked_reason", "last_error"):
        assert gone not in state, f"{gone} would keep the channel looking unlinked"
    # Unrelated state is not collateral damage.
    assert state["self_identity"] == {"user_id": "42"}


def test_repair_works_when_no_adapter_is_registered(monkeypatch):
    """The common case: the run loop already died, so nothing is registered.

    The session still has to be erased, which is why the handler falls back to
    building a throwaway adapter purely to resolve its own paths.
    """
    store = _Store()
    spy = _SpyAdapter()
    registry = _Registry(None)
    _patch(monkeypatch, registry, spy_adapter=spy)

    resp = _run(store)

    assert resp.status_code == 200
    assert spy.resets == 1
    assert registry.calls == ["stop:ch1", "start:ch1"]


def test_repair_reports_an_adapter_that_will_not_restart(monkeypatch):
    """Session cleared but the adapter refused to come up: say why.

    A bare 200 here would send the UI back to the auth stream to wait on an
    adapter that never started — the same silent hang, one step later.
    """
    store = _Store()
    registry = _Registry(_SpyAdapter(), start_disables=True)
    _patch(monkeypatch, registry)

    resp = _run(store)

    assert resp.status_code == 409
    assert json.loads(resp.body)["error"] == "node missing"


def test_repair_is_scoped_to_the_callers_profile(monkeypatch):
    store = _Store()
    adapter = _SpyAdapter()
    registry = _Registry(adapter)
    _patch(monkeypatch, registry)

    resp = _run(store, username="someone-else")

    assert resp.status_code == 403
    assert adapter.resets == 0
    assert registry.calls == []


def test_repair_requires_authentication(monkeypatch):
    store = _Store()
    registry = _Registry(_SpyAdapter())
    _patch(monkeypatch, registry)
    handler = _handler(store, _REPAIR, "POST")
    req = SimpleNamespace(
        method="POST",
        user=SimpleNamespace(is_authenticated=False, username=""),
        path_params={"channel_id": "ch1"},
    )

    resp = asyncio.run(handler(req))

    assert resp.status_code == 401
    assert registry.calls == []
