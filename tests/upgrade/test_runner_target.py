"""Tests for the test-channel targeted apply (Updates-page version picker).

``runner.apply(target_version=…)`` on the test channel installs a *specific*
RC — possibly older than the current install — bypassing the "must be newer"
gate. On production/dev the historical "target must equal latest" guard stays.
"""

from __future__ import annotations

import pytest

from app.upgrade import manifest, runner


def _release(version: str, *, min_from: str = "0.0.0") -> manifest.ReleaseInfo:
    return manifest.ReleaseInfo(
        version=version,
        tag_name=f"v{version}",
        name=version,
        html_url="",
        body="",
        asset_url=None,
        min_supported_upgrade_from=min_from,
        channel="test",
    )


def test_apply_target_on_test_bypasses_is_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Current install is NEWER than the target (a sideways/downgrade switch
    # between PR RCs). The latest-based flow would say up_to_date and skip;
    # the targeted path must still install.
    monkeypatch.setattr(runner, "get_channel", lambda: "test")
    monkeypatch.setattr(runner, "CURRENT_VERSION", "0.2.9rc2.dev1")
    monkeypatch.setattr(manifest, "resolve_release", lambda v, **kw: _release("0.2.9rc1.dev2"))
    # check() must NOT be consulted on the targeted path.
    monkeypatch.setattr(runner, "check", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("check() should not run")))

    captured: dict[str, str] = {}

    def fake_locked(release, callback):
        captured["version"] = release.version
        return True

    monkeypatch.setattr(runner, "_apply_locked", fake_locked)

    assert runner.apply(target_version="0.2.9rc1.dev2") is True
    assert captured["version"] == "0.2.9rc1.dev2"


def test_apply_target_noop_when_already_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "get_channel", lambda: "test")
    monkeypatch.setattr(runner, "CURRENT_VERSION", "0.2.9rc1.dev2")
    monkeypatch.setattr(manifest, "resolve_release", lambda v, **kw: _release("0.2.9rc1.dev2"))

    called = {"locked": False}
    monkeypatch.setattr(runner, "_apply_locked", lambda r, cb: called.__setitem__("locked", True) or True)

    assert runner.apply(target_version="0.2.9rc1.dev2") is True
    assert called["locked"] is False  # nothing to install


def test_apply_target_warns_but_proceeds_when_min_supported_too_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test-channel targeted installs no longer hard-reject when the
    # target's floor is above current — the maintainer's explicit pick
    # is the contract, and backup/rollback in _apply_locked is the
    # actual safety net. We DO emit a WARNING event so the log makes
    # the mismatch visible.
    monkeypatch.setattr(runner, "get_channel", lambda: "test")
    monkeypatch.setattr(runner, "CURRENT_VERSION", "0.1.0")
    monkeypatch.setattr(
        manifest, "resolve_release", lambda v, **kw: _release("0.2.9rc1.dev2", min_from="0.2.0")
    )

    called = {"locked": False}
    monkeypatch.setattr(runner, "_apply_locked", lambda r, cb: called.__setitem__("locked", True) or True)

    events: list[runner.UpgradeEvent] = []
    assert runner.apply(target_version="0.2.9rc1.dev2", callback=events.append) is True
    assert called["locked"] is True  # falls through to the locked install path

    # A single WARNING event is emitted naming the floor and current
    # version so the modal log explains the risk.
    warn_events = [e for e in events if "WARNING" in e.message]
    assert len(warn_events) == 1
    assert "min_supported_upgrade_from=0.2.0" in warn_events[0].message
    assert "0.1.0" in warn_events[0].message  # the current version
    # The WARNING is informational, not a failure — ok stays True so
    # detached.py's last-failure capture doesn't latch onto it.
    assert warn_events[0].ok is True


def test_apply_target_on_production_keeps_latest_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off the test channel, a target that isn't the latest is rejected and
    # nothing is installed (the historical safety guard).
    monkeypatch.setattr(runner, "get_channel", lambda: "production")
    monkeypatch.setattr(runner, "check", lambda *a, **kw: (_release("1.0.0"), "available"))

    called = {"locked": False}
    monkeypatch.setattr(runner, "_apply_locked", lambda r, cb: called.__setitem__("locked", True) or True)
    # resolve_release must NOT be used on the production path.
    monkeypatch.setattr(
        manifest, "resolve_release",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("resolve_release is test-only")),
    )

    assert runner.apply(target_version="0.9.0") is False
    assert called["locked"] is False
