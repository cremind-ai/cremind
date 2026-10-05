"""The Visual C++ runtime PyTorch needs on Windows (app/embeddings/vc_runtime.py).

torch 2.9+ crashes initialising ``c10.dll`` against an ``msvcp140.dll`` older
than 14.40 (``[WinError 1114]``), so before the embedding providers import
torch, ``prepare`` either leaves a current runtime alone or loads Cremind's
private copy, and ``explain_load_error`` turns the failures it can't prevent
into what to do. The decision tests fake the Win32 seams, so they run
anywhere; the ``win32`` ones read real DLLs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

import app.embeddings as embeddings_pkg
from app.embeddings import vc_runtime
from app.embeddings.vc_runtime import Runtime

SYSTEM_DLL = r"C:\Windows\System32\msvcp140.dll"
PRIVATE_DLL = r"C:\Users\admin\.cremind\venv\msvcp140.dll"
BOTH_COMPANIONS = ("vcruntime140_threads.dll", "msvcp140_atomic_wait.dll")

OLD_SYSTEM = Runtime(SYSTEM_DLL, (14, 29, 30133, 0))
CURRENT_SYSTEM = Runtime(SYSTEM_DLL, (14, 50, 35719, 0))
NO_SYSTEM = Runtime(SYSTEM_DLL, None, BOTH_COMPANIONS)
PRIVATE = Runtime(PRIVATE_DLL, (14, 44, 35211, 0))
NO_PRIVATE = Runtime(PRIVATE_DLL, None, BOTH_COMPANIONS)


class FakeWindows:
    """Stands in for every Win32 seam and records what ``prepare`` did."""

    def __init__(
        self,
        monkeypatch,
        *,
        loaded: Runtime | None = None,
        system: Runtime = OLD_SYSTEM,
        private: Runtime = NO_PRIVATE,
        install_ok: bool = True,
        load_ok: bool = True,
    ) -> None:
        self.loaded = loaded
        self.system = system
        self.private = private
        self.install_ok = install_ok
        self.load_ok = load_ok
        self.installs = 0
        self.loads: list[Runtime] = []
        monkeypatch.setattr(vc_runtime, "_WINDOWS", True)
        monkeypatch.setattr(vc_runtime, "_loaded_runtime", lambda: self.loaded)
        monkeypatch.setattr(vc_runtime, "_system_runtime", lambda: self.system)
        monkeypatch.setattr(vc_runtime, "_private_runtime", lambda: self.private)
        monkeypatch.setattr(vc_runtime, "_install_private_runtime", self._install)
        monkeypatch.setattr(vc_runtime, "_load_private", self._load)

    def _install(self) -> bool:
        self.installs += 1
        if self.install_ok:
            self.private = PRIVATE
        return self.install_ok

    def _load(self, private: Runtime) -> bool:
        self.loads.append(private)
        if self.load_ok:
            self.loaded = private
        return self.load_ok


def _dll_error(winerror: int = 1114) -> OSError:
    exc = OSError(22, "A dynamic link library (DLL) initialization routine failed.")
    exc.winerror = winerror
    return exc


# ── Runtime ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("version", "missing", "current"),
    [
        ((14, 40, 33810, 0), (), True),
        ((14, 50, 35719, 0), (), True),
        ((15, 0, 0, 0), (), True),
        ((14, 39, 33523, 0), (), False),
        ((14, 29, 30133, 0), (), False),
        (None, (), False),
        ((14, 44, 35211, 0), ("vcruntime140_threads.dll",), False),
    ],
)
def test_a_runtime_is_current_from_14_40_with_every_companion(version, missing, current):
    assert Runtime(SYSTEM_DLL, version, missing).current is current


def test_a_runtime_describes_what_this_pc_has():
    assert OLD_SYSTEM.describe() == "version 14.29.30133.0"
    assert NO_SYSTEM.describe() == "none installed"
    # Too old says it all; the missing companions only explain a new-enough one.
    old_and_incomplete = Runtime(SYSTEM_DLL, (14, 34, 31931, 0), ("vcruntime140_threads.dll",))
    assert old_and_incomplete.describe() == "version 14.34.31931.0"
    incomplete = Runtime(SYSTEM_DLL, (14, 44, 35211, 0), ("vcruntime140_threads.dll",))
    assert incomplete.describe() == "version 14.44.35211.0 without vcruntime140_threads.dll"


# ── prepare ─────────────────────────────────────────────────────────────────

def test_off_windows_prepare_touches_nothing(monkeypatch):
    def boom():
        raise AssertionError("a Win32 seam was called off Windows")

    monkeypatch.setattr(vc_runtime, "_WINDOWS", False)
    monkeypatch.setattr(vc_runtime, "_loaded_runtime", boom)
    monkeypatch.setattr(vc_runtime, "_system_runtime", boom)
    assert vc_runtime.prepare() == "n/a"


def test_a_runtime_already_in_the_process_is_left_alone(monkeypatch):
    fake = FakeWindows(monkeypatch, loaded=OLD_SYSTEM)
    assert vc_runtime.prepare() == "already-loaded"
    assert (fake.installs, fake.loads) == (0, [])


def test_a_current_system_runtime_is_used_as_is(monkeypatch):
    fake = FakeWindows(monkeypatch, system=CURRENT_SYSTEM)
    assert vc_runtime.prepare() == "system"
    assert (fake.installs, fake.loads) == (0, [])


def test_an_old_system_runtime_loads_the_private_one_already_installed(monkeypatch):
    fake = FakeWindows(monkeypatch, private=PRIVATE)
    assert vc_runtime.prepare() == "private"
    assert fake.installs == 0
    assert fake.loads == [PRIVATE]


def test_an_old_system_runtime_installs_the_private_one_then_loads_it(monkeypatch):
    fake = FakeWindows(monkeypatch)
    assert vc_runtime.prepare() == "private"
    assert fake.installs == 1
    assert fake.loads == [PRIVATE]


def test_no_system_runtime_at_all_gets_the_private_one(monkeypatch):
    fake = FakeWindows(monkeypatch, system=NO_SYSTEM)
    assert vc_runtime.prepare() == "private"
    assert fake.loads == [PRIVATE]


def test_a_system_runtime_without_the_threads_dll_counts_as_too_old(monkeypatch):
    # 14.38 brought vcruntime140_threads.dll, which torch 2.13+ imports.
    incomplete = Runtime(SYSTEM_DLL, (14, 44, 35211, 0), ("vcruntime140_threads.dll",))
    fake = FakeWindows(monkeypatch, system=incomplete, private=PRIVATE)
    assert vc_runtime.prepare() == "private"
    assert fake.loads == [PRIVATE]


def test_prepare_without_install_never_runs_pip(monkeypatch):
    fake = FakeWindows(monkeypatch)
    assert vc_runtime.prepare(install=False) == "outdated"
    assert (fake.installs, fake.loads) == (0, [])


def test_a_failed_install_reports_outdated_and_loads_nothing(monkeypatch):
    fake = FakeWindows(monkeypatch, install_ok=False)
    assert vc_runtime.prepare() == "outdated"
    assert fake.installs == 1
    assert fake.loads == []


def test_a_private_runtime_that_wont_load_reports_outdated(monkeypatch):
    FakeWindows(monkeypatch, private=PRIVATE, load_ok=False)
    assert vc_runtime.prepare() == "outdated"


def test_once_loaded_a_second_prepare_changes_nothing(monkeypatch):
    fake = FakeWindows(monkeypatch)
    assert vc_runtime.prepare() == "private"
    assert vc_runtime.prepare() == "already-loaded"
    assert (fake.installs, len(fake.loads)) == (1, 1)


def test_prepare_never_raises(monkeypatch):
    FakeWindows(monkeypatch)

    def broken():
        raise OSError("GetSystemDirectoryW blew up")

    monkeypatch.setattr(vc_runtime, "_system_runtime", broken)
    assert vc_runtime.prepare() == "unknown"


# ── explain_load_error ──────────────────────────────────────────────────────

def test_an_outdated_runtime_explains_the_redistributable_and_a_restart(monkeypatch):
    FakeWindows(monkeypatch)
    hint = vc_runtime.explain_load_error(_dll_error(1114), "outdated")
    assert hint is not None
    assert "Microsoft Visual C++ runtime 14.40 or newer" in hint
    assert "this PC has version 14.29.30133.0" in hint
    assert vc_runtime.REDIST_URL in hint
    assert hint.endswith("then restart Cremind.")


def test_a_missing_runtime_says_none_is_installed(monkeypatch):
    FakeWindows(monkeypatch, system=NO_SYSTEM)
    hint = vc_runtime.explain_load_error(_dll_error(126), "outdated")
    assert "this PC has none installed" in hint


def test_an_old_runtime_loaded_first_asks_for_a_restart_before_the_installer(monkeypatch):
    stale = Runtime(r"C:\Program Files\Other\msvcp140.dll", (14, 34, 31931, 0))
    FakeWindows(monkeypatch, loaded=stale)
    hint = vc_runtime.explain_load_error(_dll_error(1114), "already-loaded")
    assert "had already loaded version 14.34.31931.0" in hint
    assert stale.path in hint
    assert hint.index("Restart Cremind") < hint.index(vc_runtime.REDIST_URL)


def test_a_current_runtime_loaded_first_is_not_blamed(monkeypatch):
    FakeWindows(monkeypatch, loaded=CURRENT_SYSTEM, system=CURRENT_SYSTEM)
    assert vc_runtime.explain_load_error(_dll_error(1114), "already-loaded") is None


def test_a_loaded_runtime_of_unknown_version_is_not_blamed(monkeypatch):
    FakeWindows(monkeypatch, loaded=Runtime(SYSTEM_DLL, None))
    assert vc_runtime.explain_load_error(_dll_error(1114), "already-loaded") is None


@pytest.mark.parametrize("status", ["system", "private", "unknown", "n/a"])
def test_a_runtime_that_was_fine_leaves_torchs_own_error(monkeypatch, status):
    FakeWindows(monkeypatch)
    assert vc_runtime.explain_load_error(_dll_error(1114), status) is None


@pytest.mark.parametrize("winerror", [None, 5, 193])
def test_only_dll_load_failures_are_explained(monkeypatch, winerror):
    FakeWindows(monkeypatch)
    exc = OSError(22, "something else")
    if winerror is not None:
        exc.winerror = winerror
    assert vc_runtime.explain_load_error(exc, "outdated") is None


def test_off_windows_nothing_is_explained(monkeypatch):
    monkeypatch.setattr(vc_runtime, "_WINDOWS", False)
    assert vc_runtime.explain_load_error(_dll_error(1114), "outdated") is None


def test_explain_never_raises(monkeypatch):
    FakeWindows(monkeypatch)

    def broken():
        raise OSError("version.dll blew up")

    monkeypatch.setattr(vc_runtime, "_system_runtime", broken)
    assert vc_runtime.explain_load_error(_dll_error(1114), "outdated") is None


# ── the private runtime's install ───────────────────────────────────────────

def test_the_private_runtime_is_one_release_checked_by_hash():
    assert re.fullmatch(r"msvc-runtime==\d+\.\d+\.\d+", vc_runtime.PRIVATE_RUNTIME)
    digests = vc_runtime._PRIVATE_RUNTIME_SHA256
    assert digests and len(set(digests)) == len(digests)
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests)
    line = vc_runtime._requirements_line()
    assert line.startswith(vc_runtime.PRIVATE_RUNTIME + " ")
    assert line.count("--hash=sha256:") == len(digests)


def test_install_runs_pip_against_pypi_with_the_hashes(monkeypatch):
    import app.upgrade.runner as runner

    seen = {}

    def fake_pip_install(spec, callback, *, channel, upgrade=True):
        assert spec[0] == "-r"
        with open(spec[1], encoding="utf-8") as f:
            seen["requirements"] = f.read()
        seen.update(callback=callback, channel=channel, upgrade=upgrade)

    monkeypatch.setattr(runner, "_pip_install", fake_pip_install)
    assert vc_runtime._install_private_runtime() is True
    assert seen == {
        "requirements": vc_runtime._requirements_line() + "\n",
        "callback": None,
        # Never the test channel's Test PyPI, whatever this server runs on.
        "channel": "production",
        "upgrade": False,
    }


def test_a_failed_install_is_logged_not_raised(monkeypatch):
    import app.upgrade.runner as runner

    def failing_pip_install(spec, callback, *, channel, upgrade=True):
        raise RuntimeError("pip exited with code 1.")

    monkeypatch.setattr(runner, "_pip_install", failing_pip_install)
    assert vc_runtime._install_private_runtime() is False


# ── create_embedding_provider ───────────────────────────────────────────────

class _ProviderModule:
    class Provider:
        pass


def _wire_provider_import(monkeypatch, *, status: str, import_error: BaseException | None):
    """Point create_embedding_provider at a fake provider module."""
    calls: list[str] = []

    def fake_prepare(*, install=True):
        calls.append("prepare")
        return status

    def fake_import_module(name):
        calls.append(f"import {name}")
        if import_error is not None:
            raise import_error
        return _ProviderModule

    monkeypatch.setattr(embeddings_pkg.BaseConfig, "get_embedding_provider", staticmethod(lambda: "me5"))
    monkeypatch.setitem(embeddings_pkg._PROVIDER_REGISTRY, "me5", "fake.provider.Provider")
    monkeypatch.setattr(vc_runtime, "prepare", fake_prepare)
    monkeypatch.setattr(embeddings_pkg, "importlib", SimpleNamespace(import_module=fake_import_module))
    return calls


def test_the_runtime_is_prepared_before_the_provider_imports_torch(monkeypatch):
    calls = _wire_provider_import(monkeypatch, status="system", import_error=None)
    provider = embeddings_pkg.create_embedding_provider()
    assert isinstance(provider, _ProviderModule.Provider)
    assert calls == ["prepare", "import fake.provider"]


def test_a_runtime_failure_becomes_the_fix(monkeypatch):
    FakeWindows(monkeypatch)
    error = _dll_error(1114)
    _wire_provider_import(monkeypatch, status="outdated", import_error=error)
    with pytest.raises(RuntimeError) as caught:
        embeddings_pkg.create_embedding_provider()
    assert vc_runtime.REDIST_URL in str(caught.value)
    assert caught.value.__cause__ is error


def test_any_other_import_failure_is_raised_as_is(monkeypatch):
    FakeWindows(monkeypatch, system=CURRENT_SYSTEM)
    error = _dll_error(1114)
    _wire_provider_import(monkeypatch, status="system", import_error=error)
    with pytest.raises(OSError) as caught:
        embeddings_pkg.create_embedding_provider()
    assert caught.value is error


# ── real Win32 ──────────────────────────────────────────────────────────────

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="reads real Windows DLLs")


@windows_only
def test_file_version_reads_a_real_dll():
    system_dir = os.path.dirname(vc_runtime._system_runtime().path)
    version = vc_runtime._file_version(os.path.join(system_dir, "kernel32.dll"))
    assert version is not None and len(version) == 4 and version[0] >= 6


@windows_only
def test_file_version_of_a_missing_file_is_none(tmp_path):
    assert vc_runtime._file_version(str(tmp_path / "nope.dll")) is None


@windows_only
def test_the_system_runtime_lives_in_system32():
    path = vc_runtime._system_runtime().path
    assert os.path.normcase(path).endswith(os.path.normcase(r"System32\msvcp140.dll"))


@windows_only
def test_loaded_runtime_names_the_dll_this_process_loaded():
    system_dll = vc_runtime._system_runtime().path
    if not os.path.isfile(system_dll):
        pytest.skip("no Visual C++ runtime in System32")
    # A fresh process, so this one's loaded modules don't matter.
    code = (
        "import ctypes, sys\n"
        "from app.embeddings import vc_runtime\n"
        "assert vc_runtime._loaded_runtime() is None, 'msvcp140 loaded at startup'\n"
        "ctypes.WinDLL(sys.argv[1])\n"
        "print(vc_runtime._loaded_runtime().path)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, system_dll],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout.strip().splitlines()[-1]
    assert os.path.normcase(out) == os.path.normcase(system_dll)
