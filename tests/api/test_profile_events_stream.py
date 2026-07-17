"""Tests for the multiplexed ``/api/profile-events/stream`` endpoint.

This endpoint folds the settings-state, processes, and embedding-state
streams into the single per-profile SSE connection so an authenticated
web session holds one socket instead of up to four — otherwise a couple
of tabs saturate Chrome's HTTP/1.1 6-per-origin cap and later requests
stall with "Provisional headers are shown".

These tests pin the folded-in contract:

- Connect phase emits a ``settings-state`` wakeup, a ``processes``
  snapshot, and an ``embedding-state`` snapshot (with the spliced
  ``enabled`` flag), all *before* the ``ready`` marker, alongside the
  pre-existing ``conversations-list`` frame.
- Live phase forwards each source's bus publish as its named frame, and
  re-applies the ``enabled`` splice to live embedding frames.
- Closing the stream unsubscribes from every bus (no leaks).

The handler captures ``conversation_storage`` via closure, so we register
the route once with a stub and pull the endpoint out by path — the same
approach as ``tests/api/test_config_embedding.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import profile_events
from app.events.embedding_state_bus import (
    get_embedding_state_stream_bus,
    publish_embedding_state_changed,
)
from app.events.processes_bus import get_processes_stream_bus
from app.events.settings_state_bus import (
    get_settings_state_stream_bus,
    publish_settings_state_changed,
)


# ── harness ───────────────────────────────────────────────────────────


def _patch_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the connect-time reads hermetic.

    Patches the folded-in snapshot sources plus the two pre-existing
    connect-time reads (notifications replay + active-conversation ring)
    so the test doesn't depend on process/embedding/DB state. The
    settings/processes/embedding *buses* are left real so the live-phase
    publishes and the subscription-count assertions exercise the actual
    singletons.
    """
    monkeypatch.setattr(profile_events, "list_processes", lambda profile: [{"pid": "p1"}])
    monkeypatch.setattr(
        profile_events, "_augment_with_enabled", lambda snap: {**snap, "enabled": True},
    )
    fake_state = SimpleNamespace(
        to_dict=lambda: {
            "status": "disabled", "phase": None, "error": None,
            "ready": False, "busy": False,
        },
    )
    monkeypatch.setattr("app.config.embedding_state.embedding_state", fake_state)
    monkeypatch.setattr(
        profile_events, "get_event_notifications",
        lambda: SimpleNamespace(since=lambda profile, since_ms: []),
    )

    async def _snap(profile):
        return []

    monkeypatch.setattr(
        profile_events, "get_event_stream_bus",
        lambda: SimpleNamespace(snapshot_for_profile=_snap),
    )


def _make_handler():
    async def _list_conversations(profile, limit=500, offset=0, channel_type=None):
        return []

    storage = SimpleNamespace(list_conversations=_list_conversations)
    routes = profile_events.get_profile_events_routes(storage)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/profile-events/stream" and "GET" in route.methods:
            return route.endpoint
    raise AssertionError("/api/profile-events/stream route not registered")


def _make_request(disconnected: dict) -> Any:
    async def _is_disconnected() -> bool:
        return disconnected["v"]

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username="alice"),
        query_params={},
        is_disconnected=_is_disconnected,
    )


def _parse(chunk: bytes):
    """Parse one SSE chunk into ``(event_name, data)``; ``None`` for keepalive."""
    text = chunk.decode("utf-8")
    if text.startswith(":"):
        return None
    event_name = None
    data_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    data = json.loads("\n".join(data_lines)) if data_lines else None
    return (event_name, data)


async def _pull_event(it, timeout: float = 2.0):
    while True:
        chunk = await asyncio.wait_for(it.__anext__(), timeout)
        parsed = _parse(chunk)
        if parsed is not None:
            return parsed


async def _collect_until_ready(it, timeout: float = 2.0):
    frames = []
    while True:
        parsed = await _pull_event(it, timeout)
        frames.append(parsed)
        if parsed[0] == "ready":
            return frames


# ── tests ─────────────────────────────────────────────────────────────


def test_connect_phase_emits_folded_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch)

    async def run():
        handler = _make_handler()
        resp = await handler(_make_request({"v": False}))
        it = resp.body_iterator
        try:
            frames = await _collect_until_ready(it)
        finally:
            await it.aclose()

        names = [f[0] for f in frames]
        assert names[-1] == "ready"
        # Pre-existing frame still present.
        assert ("conversations-list", {"conversations": []}) in frames
        # Folded-in connect-time snapshots.
        assert ("settings-state", {}) in frames
        assert ("processes", {"processes": [{"pid": "p1"}]}) in frames
        emb = next(f for f in frames if f[0] == "embedding-state")
        assert emb[1]["status"] == "disabled"
        assert emb[1]["enabled"] is True  # spliced by _augment_with_enabled
        # All three fold-ins arrive before the ready marker.
        for name in ("settings-state", "processes", "embedding-state"):
            assert names.index(name) < names.index("ready")

    asyncio.run(run())


def test_live_phase_forwards_each_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch)

    async def run():
        handler = _make_handler()
        resp = await handler(_make_request({"v": False}))
        it = resp.body_iterator
        try:
            await _collect_until_ready(it)

            publish_settings_state_changed("alice")
            assert await _pull_event(it) == ("settings-state", {})

            get_processes_stream_bus().publish("alice", {"processes": [{"pid": "p2"}]})
            assert await _pull_event(it) == ("processes", {"processes": [{"pid": "p2"}]})

            publish_embedding_state_changed({
                "status": "ready", "phase": None, "error": None,
                "ready": True, "busy": False,
            })
            name, data = await _pull_event(it)
            assert name == "embedding-state"
            assert data["status"] == "ready"
            assert data["enabled"] is True  # splice applied to live frames too
        finally:
            await it.aclose()

    asyncio.run(run())


def test_stream_unsubscribes_all_buses_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sources(monkeypatch)

    async def run():
        settings_bus = get_settings_state_stream_bus()
        proc_bus = get_processes_stream_bus()
        emb_bus = get_embedding_state_stream_bus()
        emb_before = len(emb_bus._subs)

        handler = _make_handler()
        resp = await handler(_make_request({"v": False}))
        it = resp.body_iterator
        await _collect_until_ready(it)

        # Subscribed while the stream is live.
        assert len(settings_bus._subs.get("alice", [])) == 1
        assert len(proc_bus._subs.get("alice", [])) == 1
        assert len(emb_bus._subs) == emb_before + 1

        await it.aclose()

        # finally-block unsubscribed everything.
        assert "alice" not in settings_bus._subs
        assert "alice" not in proc_bus._subs
        assert len(emb_bus._subs) == emb_before

    asyncio.run(run())
