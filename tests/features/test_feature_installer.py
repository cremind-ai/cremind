"""``install_features`` updating outdated features, not just installing missing ones.

Before this, the installer skipped anything that imported, so a runtime venv
that got ``openai-codex`` 0.1.0b3 under the old pin could never be moved to
0.154 from the UI or ``cremind features install codex``. These cases pin the
update path (one pip call carrying the range, ``upgraded`` + restart state),
the Windows guards around it, and that a plain install of a missing feature
behaves exactly as before.

pip never runs: ``_pip_install`` is replaced by a fake that edits an in-memory
"venv" (which features import, which dist versions are recorded), and both the
probe and ``importlib.metadata.version`` answer from that same venv.
"""

from __future__ import annotations

import importlib.metadata
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from app.__version__ import __version__
from app.features import installer, manifest

CODEX_REQ = "openai-codex>=0.154.0,<0.155"


class FakeVenv:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installed: set[str] = set()
        self.versions: dict[str, str] = {}
        self.pip_calls: list[dict[str, Any]] = []
        # What the next pip run does to the venv; ``None`` leaves it untouched.
        self.on_pip: Any = None
        self.pip_error: Exception | None = None

        def fake_is_installed(key: str) -> bool:
            if key not in manifest.FEATURES:
                raise KeyError(key)
            return key in self.installed

        def fake_version(dist: str) -> str:
            if dist not in self.versions:
                raise importlib.metadata.PackageNotFoundError(dist)
            return self.versions[dist]

        def fake_pip_install(spec, callback, *, channel, upgrade=True) -> None:
            self.pip_calls.append({"spec": spec, "channel": channel, "upgrade": upgrade})
            if self.pip_error is not None:
                raise self.pip_error
            if self.on_pip is not None:
                self.on_pip(self)

        monkeypatch.setattr(manifest, "is_installed", fake_is_installed)
        monkeypatch.setattr(installer, "is_installed", fake_is_installed)
        monkeypatch.setattr(importlib.metadata, "version", fake_version)
        monkeypatch.setattr(installer, "_pip_install", fake_pip_install)


@pytest.fixture
def venv(monkeypatch: pytest.MonkeyPatch) -> FakeVenv:
    # Restart state is process-global by design; each case starts clean.
    monkeypatch.setattr(installer, "_restart_pending", set())
    monkeypatch.setattr(installer, "get_channel", lambda: "production")
    # Platform-independent by default; the Windows cases opt in. Only the
    # installer's own ``os`` reference is swapped, never the real module.
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix"))
    return FakeVenv(monkeypatch)


def _outdated_codex(venv: FakeVenv) -> None:
    venv.installed.add("codex")
    venv.versions["openai-codex"] = "0.1.0b3"


def _lands_0_154(v: FakeVenv) -> None:
    v.versions["openai-codex"] = "0.154.0"


def _install(keys: list[str]) -> tuple[installer.InstallResult, list[installer.InstallEvent]]:
    events: list[installer.InstallEvent] = []
    result = installer.install_features(keys, events.append)
    return result, events


def _fake_codex_modules(
    monkeypatch: pytest.MonkeyPatch, *, tasks: Any = 0, sign_ins: Any = 0,
) -> None:
    """Stand in for the codex tool modules the busy guard imports lazily.

    ``importlib.import_module`` returns a ``sys.modules`` entry as-is, so the
    guard never pulls in the real tool package here. A callable count is
    called, so a case can make it raise.
    """

    def counter(value: Any):
        return value if callable(value) else (lambda: value)

    monkeypatch.setitem(
        sys.modules, "app.tools.builtin.codex_runner", SimpleNamespace(busy_count=counter(tasks)),
    )
    monkeypatch.setitem(
        sys.modules, "app.tools.builtin.codex_login", SimpleNamespace(active_count=counter(sign_ins)),
    )


# ── updating an outdated feature ─────────────────────────────────────────


def test_update_passes_the_range_and_marks_restart(venv: FakeVenv) -> None:
    _outdated_codex(venv)
    venv.on_pip = _lands_0_154

    result, events = _install(["codex"])

    assert venv.pip_calls == [{
        "spec": [f"cremind[codex]=={__version__}", CODEX_REQ],
        "channel": "production",
        "upgrade": False,
    }]
    assert result.upgraded == ["codex"]
    assert result.installed == []
    assert result.failed == []
    assert result.already_present == []
    assert result.error is None
    # codex's ``requires_restart`` is False (a first install loads in-process),
    # but the old SDK is already in memory after an update.
    assert result.restart_required is True
    assert installer.restart_pending("codex") is True
    assert installer.restart_pending("claude_code") is False

    messages = [e.message for e in events]
    assert "openai-codex 0.1.0b3 does not satisfy openai-codex>=0.154.0,<0.155 - updating codex" in messages
    assert f"pip install cremind[codex]=={__version__} {CODEX_REQ}" in messages
    done = events[-1]
    assert done.kind == "done" and done.ok is True
    assert "Updated: codex" in done.message
    assert done.meta["upgraded"] == ["codex"]


def test_dev_channel_updates_through_the_range_alone(
    venv: FakeVenv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "get_channel", lambda: "dev")
    _outdated_codex(venv)
    venv.on_pip = _lands_0_154

    result, _ = _install(["codex"])

    assert venv.pip_calls[0]["spec"] == ["cremind", CODEX_REQ]
    assert result.upgraded == ["codex"]


def test_nothing_outdated_means_no_pip(venv: FakeVenv) -> None:
    venv.installed.add("codex")
    venv.versions["openai-codex"] = "0.154.0"

    result, events = _install(["codex"])

    assert venv.pip_calls == []
    assert result.already_present == ["codex"]
    assert result.upgraded == [] and result.installed == []
    assert result.restart_required is False
    assert installer.restart_pending("codex") is False
    assert events[-1].kind == "done"
    assert events[-1].message == "All requested features already installed."


def test_still_outdated_after_pip_is_a_failure(venv: FakeVenv) -> None:
    """pip can exit 0 and keep the old version; importable is not enough."""
    _outdated_codex(venv)
    venv.on_pip = None

    result, events = _install(["codex"])

    assert len(venv.pip_calls) == 1
    assert result.failed == ["codex"]
    assert result.upgraded == []
    assert result.restart_required is False
    assert installer.restart_pending("codex") is False
    assert "openai-codex 0.1.0b3 still installed" in (result.error or "")
    assert events[-1].kind == "done" and events[-1].ok is False


def test_missing_and_outdated_share_one_pip_call(venv: FakeVenv) -> None:
    _outdated_codex(venv)

    def lands(v: FakeVenv) -> None:
        _lands_0_154(v)
        v.installed.add("vectorstore.qdrant")

    venv.on_pip = lands

    result, _ = _install(["vectorstore.qdrant", "codex"])

    assert venv.pip_calls[0]["spec"] == [
        f"cremind[vectorstore-qdrant,codex]=={__version__}",
        CODEX_REQ,
    ]
    assert result.installed == ["vectorstore.qdrant"]
    assert result.upgraded == ["codex"]
    assert result.restart_required is True


# ── installing a missing feature (unchanged behaviour) ───────────────────


def test_missing_feature_installs_as_before(venv: FakeVenv) -> None:
    venv.on_pip = lambda v: v.installed.add("vectorstore.qdrant")

    result, events = _install(["vectorstore.qdrant"])

    assert venv.pip_calls[0]["spec"] == [f"cremind[vectorstore-qdrant]=={__version__}"]
    assert venv.pip_calls[0]["upgrade"] is False
    assert result.installed == ["vectorstore.qdrant"]
    assert result.upgraded == []
    assert result.restart_required is False
    assert installer.restart_pending("vectorstore.qdrant") is False
    assert events[-1].message == "Install complete."


def test_a_first_codex_install_needs_no_restart(venv: FakeVenv) -> None:
    def lands(v: FakeVenv) -> None:
        v.installed.add("codex")
        _lands_0_154(v)

    venv.on_pip = lands

    result, _ = _install(["codex"])

    assert result.installed == ["codex"]
    assert result.upgraded == []
    assert result.restart_required is False
    assert installer.restart_pending("codex") is False


def test_a_restart_feature_still_reports_restart(venv: FakeVenv) -> None:
    venv.on_pip = lambda v: v.installed.add("embedding.me5")

    result, events = _install(["embedding.me5"])

    assert result.installed == ["embedding.me5"]
    assert result.restart_required is True
    assert "Restart required for: embedding.me5" in events[-1].message
    # ``restart_pending`` tracks in-place updates only.
    assert installer.restart_pending("embedding.me5") is False


def test_pip_failure_on_a_missing_feature_fails_it(venv: FakeVenv) -> None:
    venv.pip_error = RuntimeError("pip exited 1")

    result, events = _install(["vectorstore.qdrant"])

    assert result.failed == ["vectorstore.qdrant"]
    assert result.error == "pip exited 1"
    assert any(e.kind == "error" and e.message == "pip install failed: pip exited 1" for e in events)


def test_unknown_feature_is_rejected_before_pip(venv: FakeVenv) -> None:
    result, _ = _install(["nope"])

    assert venv.pip_calls == []
    assert result.error == "Unknown feature: 'nope'"


# ── Windows: a running codex binary can't be replaced ────────────────────


@pytest.mark.parametrize(("tasks", "sign_ins"), [(1, 0), (0, 2)])
def test_windows_refuses_to_update_busy_codex(
    venv: FakeVenv, monkeypatch: pytest.MonkeyPatch, tasks: int, sign_ins: int,
) -> None:
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt"))
    _fake_codex_modules(monkeypatch, tasks=tasks, sign_ins=sign_ins)
    _outdated_codex(venv)
    venv.on_pip = _lands_0_154

    result, events = _install(["codex"])

    assert venv.pip_calls == []
    assert result.failed == ["codex"]
    assert result.upgraded == []
    assert installer.restart_pending("codex") is False
    assert f"{tasks} running task(s)" in (result.error or "")
    assert f"{sign_ins} sign-in(s)" in (result.error or "")
    assert any(e.kind == "error" and e.ok is False for e in events)


def test_windows_busy_check_fails_open(venv: FakeVenv, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt"))
    _fake_codex_modules(monkeypatch, tasks=boom, sign_ins=boom)
    _outdated_codex(venv)
    venv.on_pip = _lands_0_154

    result, _ = _install(["codex"])

    assert len(venv.pip_calls) == 1
    assert result.upgraded == ["codex"]


def test_busy_guard_is_windows_only(venv: FakeVenv, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex_modules(monkeypatch, tasks=3, sign_ins=1)
    _outdated_codex(venv)
    venv.on_pip = _lands_0_154

    result, _ = _install(["codex"])

    assert len(venv.pip_calls) == 1
    assert result.upgraded == ["codex"]


def test_busy_guard_skips_a_first_install(venv: FakeVenv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing old is running when codex isn't installed yet."""
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt"))
    _fake_codex_modules(monkeypatch, tasks=1)

    def lands(v: FakeVenv) -> None:
        v.installed.add("codex")
        _lands_0_154(v)

    venv.on_pip = lands

    result, _ = _install(["codex"])

    assert len(venv.pip_calls) == 1
    assert result.installed == ["codex"]


@pytest.mark.parametrize(
    "pip_output",
    [
        "ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: 'codex.exe'",
        "ERROR: Could not install packages due to an OSError: [WinError 32] The process cannot "
        "access the file because it is being used by another process",
        "PermissionError: Access is denied",
    ],
)
def test_file_lock_failure_on_update_adds_the_restart_hint(venv: FakeVenv, pip_output: str) -> None:
    _outdated_codex(venv)
    venv.pip_error = RuntimeError(pip_output)

    result, events = _install(["codex"])

    hint = "Restart the Cremind server, then update before starting Codex."
    assert result.failed == ["codex"]
    assert result.error is not None
    assert result.error.startswith(pip_output)
    assert hint in result.error
    assert any(e.kind == "error" and hint in e.message for e in events)


def test_file_lock_hint_is_only_for_updates(venv: FakeVenv) -> None:
    """A first install has no old binary running; the advice would mislead."""
    venv.pip_error = RuntimeError("[WinError 5] Access is denied")

    result, _ = _install(["codex"])

    assert result.error == "[WinError 5] Access is denied"


def test_winerror_codes_match_exactly(venv: FakeVenv) -> None:
    _outdated_codex(venv)
    venv.pip_error = RuntimeError("[WinError 53] The network path was not found")

    result, _ = _install(["codex"])

    assert result.error == "[WinError 53] The network path was not found"


# ── feature_status ───────────────────────────────────────────────────────


def test_feature_status_reports_versions_and_restart_state(venv: FakeVenv) -> None:
    _outdated_codex(venv)

    before = installer.feature_status()["codex"]
    assert before == {
        "installed": True,
        "requires_restart_after_install": False,
        "extras": ["codex"],
        "outdated": True,
        "required": [CODEX_REQ],
        "installed_versions": {"openai-codex": "0.1.0b3"},
        "restart_pending": False,
    }

    venv.on_pip = _lands_0_154
    _install(["codex"])

    after = installer.feature_status()["codex"]
    assert after["outdated"] is False
    assert after["installed_versions"] == {"openai-codex": "0.154.0"}
    assert after["restart_pending"] is True


def test_feature_status_for_a_feature_without_ranges(venv: FakeVenv) -> None:
    status = installer.feature_status()

    assert set(status) == set(manifest.FEATURES)
    assert status["browser"] == {
        "installed": False,
        "requires_restart_after_install": True,
        "extras": ["browser"],
        "outdated": False,
        "required": [],
        "installed_versions": {},
        "restart_pending": False,
    }
