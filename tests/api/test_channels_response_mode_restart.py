"""Changing "Reply detail" has to reach the adapter that is already running.

A ``BaseChannelAdapter`` reads ``response_mode`` off the channel dict it was
constructed with, and only ``restart_for_channel`` ever replaces that dict. So a
PATCH that persists the new value without restarting saves the setting and
changes nothing — the reported symptom, where "Answer with steps" was ticked and
replies kept arriving condensed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import app.api.channels as channels_api
from app.api.channels import get_channel_routes


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(body, username="p1", channel_id="ch1"):
    async def _json():
        return body

    return SimpleNamespace(
        method="PATCH",
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params={"channel_id": channel_id},
        json=_json,
    )


class _Store:
    def __init__(self):
        self.row = {
            "id": "ch1", "profile": "p1", "channel_type": "telegram",
            "mode": "bot", "response_mode": "normal", "config": {},
        }

    async def get_channel(self, cid):
        return dict(self.row) if cid == self.row["id"] else None

    async def update_channel(self, cid, **fields):
        self.row.update(fields)
        return dict(self.row)


class _Registry:
    def __init__(self):
        self.restarted: list[str] = []

    async def restart_for_channel(self, cid, *, install_if_missing=False):
        self.restarted.append(cid)
        return None

    def status_for(self, _cid):
        # Read by ``_decorate`` when the endpoint renders its response.
        return "running"


def _patch(monkeypatch):
    registry = _Registry()
    monkeypatch.setattr(channels_api, "get_channel_registry", lambda: registry)
    return registry


def _run(store, body):
    handler = _handler(store, "/api/channels/{channel_id}", "PATCH")
    return asyncio.run(handler(_req(body)))


def test_switching_reply_detail_restarts_the_adapter(monkeypatch):
    store = _Store()
    registry = _patch(monkeypatch)

    resp = _run(store, {"response_mode": "detail"})

    assert resp.status_code == 200
    assert store.row["response_mode"] == "detail"
    assert registry.restarted == ["ch1"], (
        "the running adapter keeps serving its start-time snapshot, so a "
        "response_mode change that does not restart it never takes effect"
    )


def test_an_invalid_reply_detail_is_rejected_and_restarts_nothing(monkeypatch):
    store = _Store()
    registry = _patch(monkeypatch)

    resp = _run(store, {"response_mode": "verbose"})

    assert resp.status_code == 400
    assert json.loads(resp.body)["error"].startswith("response_mode must be")
    assert store.row["response_mode"] == "normal"
    assert registry.restarted == []


def test_a_patch_that_changes_nothing_runtime_does_not_restart(monkeypatch):
    """The restart is not free — it drops the adapter's group counters and its
    dedupe ring — so it stays gated on the keys that actually need it."""
    store = _Store()
    registry = _patch(monkeypatch)

    resp = _run(store, {})

    assert resp.status_code == 200
    assert registry.restarted == []
