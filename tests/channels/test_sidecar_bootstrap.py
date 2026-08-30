"""Sidecar dependency bootstrap: freshness, install, and the adapter guard.

Covers the failure that broke Zalo on Kubernetes: the wheel shipped without
``package-lock.json``, ``npm ci`` cannot run without one, and the boot-time
installer quietly gave up — leaving a channel that could never start and an
error telling the user to restart, which never helped.

Everything here is hermetic: npm is faked through ``shutil.which`` and
``subprocess.Popen``, so no test touches the network or a real node install.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading

import pytest

from app.channels.exceptions import ChannelNotImplemented
from app.channels.sidecars import bootstrap


# ── helpers ───────────────────────────────────────────────────────────────


def _lock_body(packages: dict[str, str] | None) -> str:
    """A lockfile/marker body in npm's shape, from ``{path: version}``."""
    body: dict = {"name": "x", "lockfileVersion": 3, "packages": {"": {"name": "x"}}}
    for path, version in (packages or {}).items():
        body["packages"][path] = {"version": version}
    return json.dumps(body)


def make_sidecar(
    tmp_path,
    *,
    name: str = "zalo",
    lock: bool = True,
    node_modules: bool = True,
    marker: bool = True,
    packages: dict[str, str] | None = None,
):
    """Build a sidecar dir on disk with the requested pieces present.

    ``packages`` seeds *what npm installed* (the marker). Pair it with
    :func:`write_lock` to describe what the lockfile asks for; passing the
    same mapping to both is the fresh case.
    """
    d = tmp_path / name
    d.mkdir()
    (d / "package.json").write_text('{"name": "x"}')
    (d / "index.js").write_text("// sidecar")
    if lock:
        (d / "package-lock.json").write_text(_lock_body(packages))
    if node_modules:
        nm = d / "node_modules"
        nm.mkdir()
        if marker:
            (nm / ".package-lock.json").write_text(_lock_body(packages))
    return d


def write_lock(sidecar_dir, packages: dict[str, str]):
    """Rewrite only the lockfile, leaving the install marker as it was."""
    (sidecar_dir / "package-lock.json").write_text(_lock_body(packages))


class FakePopen:
    """Stand-in for a finished npm process."""

    def __init__(self, rc: int = 0, lines=(), timeout: bool = False):
        self._rc = rc
        self._timeout = timeout
        self.stdout = iter(lines)
        self.killed = False

    def wait(self, timeout=None):
        # Only the timed-out wait raises; the reaping wait() after kill()
        # returns, exactly as subprocess behaves.
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired(cmd="npm", timeout=timeout or 0)
        return self._rc

    def kill(self):
        self.killed = True


@pytest.fixture
def npm_spy(monkeypatch):
    """Fake an npm on PATH; record the argv every install invokes."""
    calls: list[list[str]] = []
    state = {"proc": FakePopen()}

    def fake_which(cmd):
        return f"/usr/bin/{cmd}"

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return state["proc"]

    monkeypatch.setattr(bootstrap.shutil, "which", fake_which)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", fake_popen)
    return calls, state


@pytest.fixture(autouse=True)
def _clear_install_locks():
    """Keep the module-level per-directory lock table from leaking between tests."""
    bootstrap._INSTALL_LOCKS.clear()
    yield
    bootstrap._INSTALL_LOCKS.clear()


# ── is_install_fresh ──────────────────────────────────────────────────────


def test_missing_package_json_is_not_a_sidecar(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert bootstrap.is_install_fresh(d) == (False, "package.json missing")


def test_missing_node_modules_is_stale(tmp_path):
    d = make_sidecar(tmp_path, node_modules=False)
    fresh, reason = bootstrap.is_install_fresh(d)
    assert not fresh
    assert reason == "node_modules missing"


def test_incomplete_install_is_stale(tmp_path):
    d = make_sidecar(tmp_path, marker=False)
    fresh, reason = bootstrap.is_install_fresh(d)
    assert not fresh
    assert "incomplete install" in reason


def test_complete_install_without_lockfile_is_fresh(tmp_path):
    """The regression this whole change exists for.

    node resolves imports out of node_modules and never reads the lockfile,
    so an install healed by the ``npm install`` fallback must not be
    re-installed on every boot.
    """
    d = make_sidecar(tmp_path, lock=False)
    assert bootstrap.is_install_fresh(d) == (True, "fresh")


def test_upgraded_lockfile_wanting_a_new_version_is_stale(tmp_path):
    """A wheel upgrade that really changes a dependency reconciles onto npm ci."""
    d = make_sidecar(tmp_path, packages={"node_modules/ws": "8.20.0"})
    write_lock(d, {"node_modules/ws": "8.21.3"})
    fresh, reason = bootstrap.is_install_fresh(d)
    assert not fresh
    assert reason == "node_modules/ws is installed at 8.20.0, package-lock.json wants 8.21.3"


def test_lockfile_entry_missing_from_install_is_stale(tmp_path):
    """A newly added dependency has to be installed before the tree is fresh."""
    d = make_sidecar(tmp_path, packages={"node_modules/ws": "8.20.0"})
    write_lock(d, {"node_modules/ws": "8.20.0", "node_modules/qrcode": "1.5.4"})
    fresh, reason = bootstrap.is_install_fresh(d)
    assert not fresh
    assert reason == "node_modules/qrcode is in package-lock.json but not installed"


def test_platform_gated_optional_deps_do_not_make_it_stale(tmp_path):
    """The sharp binaries are the reason this matters.

    A portable lockfile carries every ``@img/sharp-*`` variant, but npm only
    installs the one for the running platform. Requiring all of them would
    call every healthy tree stale and reinstall on every single boot.
    """
    d = make_sidecar(tmp_path, packages={"node_modules/sharp": "0.34.5"})
    lock = {
        "packages": {
            "": {"name": "x"},
            "node_modules/sharp": {"version": "0.34.5"},
            "node_modules/@img/sharp-linux-x64": {"version": "0.34.5", "optional": True},
            "node_modules/@img/sharp-win32-x64": {"version": "0.34.5", "optional": True},
        }
    }
    (d / "package-lock.json").write_text(json.dumps(lock))
    assert bootstrap.is_install_fresh(d) == (True, "fresh")


def test_touching_files_without_changing_them_stays_fresh(tmp_path):
    """pip rewrites every file it installs with a fresh mtime.

    An mtime-based rule therefore re-downloads ~66MB on every release
    upgrade even when the dependency tree is byte-identical — including for
    users who never enable a sidecar channel.
    """
    d = make_sidecar(tmp_path, packages={"node_modules/ws": "8.20.0"})
    write_lock(d, {"node_modules/ws": "8.20.0"})
    assert bootstrap.is_install_fresh(d) == (True, "fresh")

    newer = (d / "node_modules" / ".package-lock.json").stat().st_mtime + 10_000
    for name in ("package.json", "package-lock.json"):
        os.utime(d / name, (newer, newer))

    assert bootstrap.is_install_fresh(d) == (True, "fresh")


def test_unreadable_lockfile_reinstalls_rather_than_guessing(tmp_path):
    d = make_sidecar(tmp_path)
    (d / "package-lock.json").write_text("{not json")
    fresh, reason = bootstrap.is_install_fresh(d)
    assert not fresh
    assert "could not compare" in reason


def test_fully_installed_is_fresh(tmp_path):
    d = make_sidecar(tmp_path)
    assert bootstrap.is_install_fresh(d) == (True, "fresh")


# ── ensure_sidecar_installed ──────────────────────────────────────────────


def test_fresh_install_skips_npm(tmp_path, npm_spy):
    calls, _ = npm_spy
    d = make_sidecar(tmp_path)
    bootstrap.ensure_sidecar_installed(d)
    assert calls == []


def test_missing_npm_warns_and_returns(tmp_path, monkeypatch):
    """Boot must survive a host with no Node toolchain at all."""
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: None)
    d = make_sidecar(tmp_path, node_modules=False)
    bootstrap.ensure_sidecar_installed(d)  # must not raise


def test_lockfile_present_uses_npm_ci(tmp_path, npm_spy):
    calls, _ = npm_spy
    d = make_sidecar(tmp_path, node_modules=False)
    bootstrap.ensure_sidecar_installed(d)
    assert len(calls) == 1
    assert calls[0][1:] == ["ci"]


def test_lockfile_absent_falls_back_to_npm_install(tmp_path, npm_spy):
    """Heals installs from wheels that predate the lockfile shipping."""
    calls, _ = npm_spy
    d = make_sidecar(tmp_path, lock=False, node_modules=False)
    bootstrap.ensure_sidecar_installed(d)
    assert len(calls) == 1
    assert calls[0][1] == "install"


def test_nonzero_exit_raises(tmp_path, npm_spy):
    _, state = npm_spy
    state["proc"] = FakePopen(rc=1)
    d = make_sidecar(tmp_path, node_modules=False)
    with pytest.raises(bootstrap.SidecarBootstrapError, match="exit code 1"):
        bootstrap.ensure_sidecar_installed(d)


def test_timeout_kills_process_and_raises(tmp_path, npm_spy):
    _, state = npm_spy
    proc = state["proc"] = FakePopen(timeout=True)
    d = make_sidecar(tmp_path, node_modules=False)
    with pytest.raises(bootstrap.SidecarBootstrapError, match="timed out"):
        bootstrap.ensure_sidecar_installed(d, timeout_s=5)
    assert proc.killed


# ── ensure_all_sidecars_installed ─────────────────────────────────────────


def test_boot_survives_a_failing_sidecar(tmp_path, monkeypatch):
    """A dead npm registry must cost you the channel, not the server."""
    monkeypatch.setattr(bootstrap, "SIDECARS_ROOT", tmp_path)
    make_sidecar(tmp_path, name="zalo", node_modules=False)
    make_sidecar(tmp_path, name="whatsapp", node_modules=False)

    attempted: list[str] = []

    def boom(sidecar_dir, **kwargs):
        attempted.append(sidecar_dir.name)
        raise bootstrap.SidecarBootstrapError("registry unreachable")

    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", boom)
    bootstrap.ensure_all_sidecars_installed()  # must not raise

    # Every sidecar is still attempted; one failure doesn't skip the rest.
    assert sorted(attempted) == ["whatsapp", "zalo"]


# ── start_background_bootstrap ────────────────────────────────────────────


def test_background_bootstrap_does_not_block_the_caller(tmp_path, monkeypatch):
    """The regression: a cold npm ci used to delay the socket bind.

    The installers wait 10x1s for /health and then open a browser, so a
    startup that blocks on ~66MB of npm hands the user a dead link.
    """
    monkeypatch.setattr(bootstrap, "SIDECARS_ROOT", tmp_path)
    make_sidecar(tmp_path, name="zalo", node_modules=False)

    started = threading.Event()
    release = threading.Event()

    def slow(sidecar_dir, **kwargs):
        started.set()
        assert release.wait(timeout=5), "install ran but was never released"

    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", slow)

    thread = bootstrap.start_background_bootstrap()
    assert thread is not None
    # The install is genuinely in flight while we are already past the call.
    assert started.wait(timeout=5)
    assert thread.is_alive()
    assert thread.daemon, "a hung npm must never keep the process alive"

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_background_bootstrap_skips_entirely_when_everything_is_fresh(
    tmp_path, monkeypatch
):
    """The steady state: no thread, no npm, on every normal restart."""
    monkeypatch.setattr(bootstrap, "SIDECARS_ROOT", tmp_path)
    make_sidecar(tmp_path, name="zalo", packages={"node_modules/ws": "8.20.0"})

    def unexpected(sidecar_dir, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a fresh tree must not be reinstalled")

    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", unexpected)
    assert bootstrap.start_background_bootstrap() is None


def test_background_bootstrap_is_a_noop_without_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "SIDECARS_ROOT", tmp_path)
    assert bootstrap.start_background_bootstrap() is None


# ── ensure_sidecar_ready (the adapter guard) ──────────────────────────────


def test_ready_requires_node(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: None)
    d = make_sidecar(tmp_path)
    with pytest.raises(ChannelNotImplemented, match="Node.js is not installed"):
        asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))


def test_ready_reports_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    d = make_sidecar(tmp_path)
    (d / "index.js").unlink()
    with pytest.raises(ChannelNotImplemented, match="sidecar source missing"):
        asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))


def test_ready_skips_install_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    d = make_sidecar(tmp_path)
    calls = []
    monkeypatch.setattr(
        bootstrap, "ensure_sidecar_installed", lambda *a, **k: calls.append(a),
    )
    asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))
    assert calls == []


def test_ready_heals_a_stale_install(tmp_path, monkeypatch):
    """Enabling the channel installs the deps instead of just complaining."""
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    d = make_sidecar(tmp_path, node_modules=False)
    calls = []

    def fake_install(sidecar_dir, **kwargs):
        calls.append(sidecar_dir)
        nm = sidecar_dir / "node_modules"
        nm.mkdir()
        (nm / ".package-lock.json").write_text("{}")

    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", fake_install)
    asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))
    assert calls == [d]


def test_ready_explains_a_failed_install(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    d = make_sidecar(tmp_path, node_modules=False)

    def boom(sidecar_dir, **kwargs):
        raise bootstrap.SidecarBootstrapError("registry unreachable")

    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", boom)
    with pytest.raises(ChannelNotImplemented) as exc:
        asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))
    message = str(exc.value)
    assert "registry.npmjs.org" in message
    # The old message told users to restart the server, which never healed
    # this. Re-enabling the channel actually retries.
    assert "re-enable the channel" in message
    assert "Restart the server" not in message


def test_ready_explains_missing_npm(tmp_path, monkeypatch):
    """node present but npm absent: the install silently does nothing."""
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda cmd: "/usr/bin/node" if cmd == "node" else None,
    )
    d = make_sidecar(tmp_path, node_modules=False)
    with pytest.raises(ChannelNotImplemented) as exc:
        asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))
    assert "`npm` is not on PATH" in str(exc.value)


def test_ready_reports_a_still_broken_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    d = make_sidecar(tmp_path, node_modules=False)
    monkeypatch.setattr(bootstrap, "ensure_sidecar_installed", lambda *a, **k: None)
    with pytest.raises(ChannelNotImplemented, match="still not ready"):
        asyncio.run(bootstrap.ensure_sidecar_ready(d, label="Zalo"))
