"""A paused subscription is skipped at dispatch while its siblings still fire.

Covers the per-subscription pause gate added to the skill-event fan-out
(``EventManager._ProfileEventHandler._dispatch``) and the file-watcher fan-out
(``FileWatcherManager._SharedHandler._fan_out``). Pausing one subscription must
not stop the shared listener/observer that its siblings depend on.
"""

from __future__ import annotations

import asyncio

import app.events.manager as mgr
import app.events.file_watcher_manager as fwm
import app.events.run_dispatcher as run_dispatcher


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def list_by_event(self, **_kw):
        return self._rows

    def list_by_root(self, **_kw):
        return self._rows


def test_skill_event_paused_is_skipped(monkeypatch):
    rows = [
        {"id": "active", "paused": False},
        {"id": "paused", "paused": True},
    ]
    monkeypatch.setattr(mgr, "get_event_subscription_storage", lambda: _FakeStore(rows))

    fired: list[str] = []

    async def _fake_dispatch(*, sub, content):
        fired.append(sub["id"])

    monkeypatch.setattr(run_dispatcher, "dispatch_skill_event", _fake_dispatch)

    handler = mgr._ProfileEventHandler.__new__(mgr._ProfileEventHandler)
    handler._profile = "p"

    class _M:
        def resolve_skill_name(self, _profile, _skill_dir):
            return "skillA"

    handler._manager = _M()

    asyncio.run(handler._dispatch("skillA", "new_email", "body"))
    assert fired == ["active"]  # paused sibling skipped, active one fired


def test_file_watcher_paused_is_skipped(monkeypatch):
    rows = [
        {"id": "active", "paused": False},
        {"id": "paused", "paused": True},
    ]
    monkeypatch.setattr(fwm, "get_file_watcher_storage", lambda: _FakeStore(rows))
    # Isolate the pause gate from the event-type/extension filter.
    monkeypatch.setattr(fwm._SharedHandler, "_passes_filter", staticmethod(lambda sub, payload: True))

    fired: list[str] = []

    async def _fake_dispatch(*, sub, payload):
        fired.append(sub["id"])

    monkeypatch.setattr(run_dispatcher, "dispatch_file_watcher_event", _fake_dispatch)

    handler = fwm._SharedHandler.__new__(fwm._SharedHandler)
    handler._profile = "p"
    handler._root_path = "/tmp/watched"
    handler._recursive = True

    asyncio.run(handler._fan_out({"event_type": "created", "path": "/tmp/watched/x.py"}))
    assert fired == ["active"]  # paused sibling skipped, active one fired
