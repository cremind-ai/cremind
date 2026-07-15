"""Registry tests for runtime SDK install-on-connect + graceful-disable.

Covers the two defects fixed together:

1. Enabling a channel whose optional SDK isn't installed pip-installs it at
   runtime (``install_if_missing=True``) before the adapter starts, and a
   failed install disables the channel instead of raising.
2. A fatal error raised inside the detached ``_run`` task disables the channel
   via a done-callback (previously "Task exception was never retrieved").

The tests fake the adapter class and the feature installer so nothing is
actually imported or pip-installed.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels import registry as registry_mod
from app.channels.registry import ChannelRegistry
from app.features.installer import InstallResult


class _FakeStorage:
    def __init__(self) -> None:
        self.channels: dict[str, dict] = {}
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id: str, **kwargs):
        self.updates.append((channel_id, kwargs))
        row = dict(self.channels.get(channel_id) or {"id": channel_id})
        row.update(kwargs)
        self.channels[channel_id] = row
        return row

    async def get_channel(self, channel_id: str):
        return self.channels.get(channel_id)


class _FakeAdapter:
    """Minimal stand-in mirroring the bits ``start_for_channel`` touches."""

    #: ``_run`` coroutine behaviour: "park" (run forever) or "raise".
    behaviour = "park"
    instances: list["_FakeAdapter"] = []

    def __init__(self, channel: dict, storage) -> None:
        self.channel = channel
        self.storage = storage
        self._task: asyncio.Task | None = None
        _FakeAdapter.instances.append(self)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="fake-run")

    async def _run(self) -> None:
        if type(self).behaviour == "raise":
            raise RuntimeError("boom")
        await asyncio.Event().wait()  # park until cancelled

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _telegram_channel(cid: str = "c1") -> dict:
    return {
        "id": cid,
        "profile": "admin",
        "channel_type": "telegram",
        "mode": "bot",
        "enabled": True,
        "config": {"bot_token": "tok"},
        "state": {},
    }


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch: pytest.MonkeyPatch):
    """Route adapter resolution to the fake and silence the notification push."""
    _FakeAdapter.instances = []
    _FakeAdapter.behaviour = "park"
    monkeypatch.setattr(
        registry_mod, "_resolve_adapter_class", lambda ct, mode: _FakeAdapter,
    )
    notes: list[str] = []
    monkeypatch.setattr(
        registry_mod,
        "_notify_channel_disabled",
        lambda channel, reason: notes.append(reason),
    )
    return notes


def _patch_feature(
    monkeypatch: pytest.MonkeyPatch, *, installed_before: bool, result: InstallResult,
) -> dict:
    """Fake ``is_installed`` / ``install_features`` used by the registry.

    ``is_installed`` flips to ``result``-driven state after an install so the
    post-install re-probe in ``_ensure_channel_feature_installed`` is honest.
    """
    state = {"installed": installed_before, "calls": 0}

    def fake_is_installed(_key: str) -> bool:
        return state["installed"]

    def fake_install(keys, emit=None):
        state["calls"] += 1
        if not result.failed and not result.error:
            state["installed"] = True
        return result

    monkeypatch.setattr(
        "app.features.manifest.is_installed", fake_is_installed,
    )
    monkeypatch.setattr(
        "app.features.installer.install_features", fake_install,
    )
    return state


def test_start_installs_missing_feature_then_starts(monkeypatch):
    storage = _FakeStorage()
    reg = ChannelRegistry(storage)
    state = _patch_feature(
        monkeypatch,
        installed_before=False,
        result=InstallResult(restart_required=False, installed=["channel.telegram.bot"]),
    )
    ch = _telegram_channel()

    async def main():
        out = await reg.start_for_channel(ch, install_if_missing=True)
        assert out["enabled"] is True
        assert reg.status_for("c1") == "running"
        await reg.stop_for_channel("c1")

    asyncio.run(main())

    assert state["calls"] == 1  # installed exactly once
    assert len(_FakeAdapter.instances) == 1  # adapter was built + started


def test_start_skips_install_when_already_present(monkeypatch):
    storage = _FakeStorage()
    reg = ChannelRegistry(storage)
    state = _patch_feature(
        monkeypatch,
        installed_before=True,
        result=InstallResult(restart_required=False),
    )
    ch = _telegram_channel()

    async def main():
        await reg.start_for_channel(ch, install_if_missing=True)
        await reg.stop_for_channel("c1")

    asyncio.run(main())

    assert state["calls"] == 0  # idempotent: nothing to install


def test_failed_install_disables_channel(monkeypatch, _patch_registry):
    storage = _FakeStorage()
    reg = ChannelRegistry(storage)
    _patch_feature(
        monkeypatch,
        installed_before=False,
        result=InstallResult(
            restart_required=False, failed=["channel.telegram.bot"], error="offline",
        ),
    )
    ch = _telegram_channel()

    async def main():
        out = await reg.start_for_channel(ch, install_if_missing=True)
        assert out["enabled"] is False
        assert "offline" in out["state"]["last_error"]

    asyncio.run(main())

    assert not _FakeAdapter.instances  # never got to building the adapter
    assert reg.status_for("c1") == "stopped"
    # persisted disable + notification
    assert any(
        kw.get("enabled") is False and "offline" in (kw.get("state") or {}).get("last_error", "")
        for _cid, kw in storage.updates
    )
    assert _patch_registry  # a "Channel disabled" reason was pushed


def test_run_failure_disables_channel_via_done_callback(monkeypatch, _patch_registry):
    storage = _FakeStorage()
    reg = ChannelRegistry(storage)
    _FakeAdapter.behaviour = "raise"
    ch = _telegram_channel()
    storage.channels["c1"] = dict(ch)

    async def main():
        # No install path here — the package is "present"; the failure is in _run.
        await reg.start_for_channel(ch, install_if_missing=False)
        # Let the detached _run task fail and its done-callback fire.
        await asyncio.sleep(0.05)

    asyncio.run(main())

    assert reg.status_for("c1") == "stopped"
    assert any(
        kw.get("enabled") is False and "boom" in (kw.get("state") or {}).get("last_error", "")
        for _cid, kw in storage.updates
    ), storage.updates
    assert _patch_registry  # notification pushed


def test_run_failure_skips_when_unlinked(monkeypatch, _patch_registry):
    """An adapter that self-marked unlinked must not be clobbered."""
    storage = _FakeStorage()
    reg = ChannelRegistry(storage)
    _FakeAdapter.behaviour = "raise"
    ch = _telegram_channel()
    # Simulate the adapter having persisted an unlink before _run raised.
    storage.channels["c1"] = {**ch, "enabled": False, "state": {"link_status": "unlinked"}}

    async def main():
        await reg.start_for_channel(ch, install_if_missing=False)
        await asyncio.sleep(0.05)

    asyncio.run(main())

    # No generic "start failed" disable write should have been added.
    assert not any(
        "boom" in (kw.get("state") or {}).get("last_error", "")
        for _cid, kw in storage.updates
    )
