"""SourceWatcher: real watchdog events in, settled and filtered path batches out.

The end-to-end tests drive the platform's native observer on a temp folder
with short debounce/stability windows; every wait is bounded. The unit tests
feed events straight into the coalescer and step the settle loop by hand, so
stability and backoff are checked without depending on OS event timing.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.documents.discovery import watcher as wmod
from app.documents.discovery.ignore import IgnoreMatcher
from app.documents.discovery.watcher import ROOT_RESCAN, SourceWatcher, WatcherStartError

pytest.importorskip("watchdog")

WAIT_S = 10.0


class Collector:
    def __init__(self):
        self.calls: list[tuple[float, set[str], set[str]]] = []
        self.lock = threading.Lock()

    def __call__(self, changed: set[str], removed: set[str]) -> None:
        with self.lock:
            self.calls.append((time.monotonic(), set(changed), set(removed)))

    @property
    def changed(self) -> set[str]:
        with self.lock:
            return set().union(*(c for _, c, _ in self.calls)) if self.calls else set()

    @property
    def removed(self) -> set[str]:
        with self.lock:
            return set().union(*(r for _, _, r in self.calls)) if self.calls else set()

    def wait_for(self, pred, timeout: float = WAIT_S) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self):
                return True
            time.sleep(0.05)
        return pred(self)


@pytest.fixture
def watch(tmp_path):
    started: list[SourceWatcher] = []

    def make(**kw):
        col = Collector()
        m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
        w = SourceWatcher(str(tmp_path), m, col, debounce_s=kw.pop("debounce_s", 0.2), stability_s=kw.pop("stability_s", 0.2))
        w.BACKOFF_S = (0.3,)
        w.start()
        started.append(w)
        time.sleep(0.2)  # let the native watch settle before the test acts
        return w, col

    yield make
    for w in started:
        w.stop(timeout=2.0)


# ── end to end, real watchdog ──────────────────────────────────────────────


def test_create_modify_delete(watch, tmp_path):
    w, col = watch()
    f = tmp_path / "a.txt"
    f.write_text("one", encoding="utf-8")
    assert col.wait_for(lambda c: "a.txt" in c.changed)
    assert w.last_event_at > 0
    n = len(col.calls)
    f.write_text("two, longer", encoding="utf-8")
    assert col.wait_for(lambda c: len(c.calls) > n and "a.txt" in c.calls[-1][1])
    f.unlink()
    assert col.wait_for(lambda c: "a.txt" in c.removed)


def test_paths_count_as_pending_until_handed_over(watch, tmp_path):
    # Research waits for pending_count() == 0 before resolving its scope, so
    # a path must never read as settled while it is still on its way.
    w, col = watch()
    counts: list[int] = []
    deliver = w.on_paths
    w.on_paths = lambda ch, rm: (counts.append(w.pending_count()), deliver(ch, rm))
    assert w.pending_count() == 0
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    assert col.wait_for(lambda c: "a.txt" in c.changed)
    assert counts and all(n > 0 for n in counts), counts
    assert col.wait_for(lambda c: w.pending_count() == 0)


def test_rename_is_remove_plus_change(watch, tmp_path):
    (tmp_path / "old.txt").write_text("x", encoding="utf-8")
    w, col = watch()
    os.rename(tmp_path / "old.txt", tmp_path / "new.txt")
    assert col.wait_for(lambda c: "old.txt" in c.removed and "new.txt" in c.changed)


def test_directories_carry_a_trailing_slash(watch, tmp_path):
    w, col = watch()
    (tmp_path / "sub").mkdir()
    assert col.wait_for(lambda c: "sub/" in c.changed)
    (tmp_path / "sub").rmdir()
    # Windows reports a deleted directory as a deleted path without the slash.
    assert col.wait_for(lambda c: "sub/" in c.removed or "sub" in c.removed)


def test_ignored_paths_never_arrive(watch, tmp_path):
    (tmp_path / "node_modules").mkdir()
    w, col = watch()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / "scratch.tmp").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("x", encoding="utf-8")
    (tmp_path / "real.txt").write_text("x", encoding="utf-8")
    assert col.wait_for(lambda c: "real.txt" in c.changed)
    time.sleep(0.6)
    everything = col.changed | col.removed
    assert everything == {"real.txt"}


def test_cremindignore_change_asks_for_a_subtree_rescan(watch, tmp_path):
    (tmp_path / "a").mkdir()
    w, col = watch()
    (tmp_path / "x.log").write_text("x", encoding="utf-8")
    assert col.wait_for(lambda c: "x.log" in c.changed)
    (tmp_path / ".cremindignore").write_text("*.log\n", encoding="utf-8")
    assert col.wait_for(lambda c: ROOT_RESCAN in c.changed)
    (tmp_path / "a" / ".cremindignore").write_text("*.md\n", encoding="utf-8")
    assert col.wait_for(lambda c: "a/" in c.changed)
    # The matcher dropped its cache: a new .log is ignored from now on.
    (tmp_path / "y.log").write_text("y", encoding="utf-8")
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    assert col.wait_for(lambda c: "z.txt" in c.changed)
    assert "y.log" not in col.changed


def test_a_growing_file_is_delivered_after_it_settles(watch, tmp_path):
    w, col = watch(debounce_s=0.2, stability_s=0.3)
    f = tmp_path / "download.bin"
    done = threading.Event()
    finished_at = [0.0]

    def writer():
        with open(f, "ab") as fh:
            for _ in range(8):
                fh.write(b"x" * 4096)
                fh.flush()
                os.fsync(fh.fileno())
                time.sleep(0.15)
        finished_at[0] = time.monotonic()
        done.set()

    threading.Thread(target=writer, daemon=True).start()
    assert done.wait(WAIT_S)
    assert col.wait_for(lambda c: "download.bin" in c.changed)
    first = min(t for t, ch, _ in col.calls if "download.bin" in ch)
    assert first >= finished_at[0]


def test_on_paths_exception_does_not_stop_watching(tmp_path):
    calls: list[set[str]] = []

    def flaky(changed, removed):
        calls.append(set(changed))
        if len(calls) == 1:
            raise RuntimeError("caller bug")

    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    w = SourceWatcher(str(tmp_path), m, flaky, debounce_s=0.2, stability_s=0.1)
    w.start()
    try:
        time.sleep(0.2)
        (tmp_path / "one.txt").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + WAIT_S
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        (tmp_path / "two.txt").write_text("2", encoding="utf-8")
        while not any("two.txt" in c for c in calls) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert any("two.txt" in c for c in calls)
        assert w.is_alive()
    finally:
        w.stop()


def test_start_stop_idempotent_and_fast(tmp_path):
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    w = SourceWatcher(str(tmp_path), m, lambda c, r: None)
    w.stop()  # before start: no-op
    w.start()
    w.start()  # already running: no-op
    assert w.is_alive()
    t0 = time.monotonic()
    w.stop(timeout=0.5)
    assert time.monotonic() - t0 < 1.5
    assert not w.is_alive()
    w.stop()
    w.start()  # restartable
    assert w.is_alive()
    w.stop()


def test_start_raises_when_the_root_cannot_be_watched(tmp_path):
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    w = SourceWatcher(str(tmp_path / "missing"), m, lambda c, r: None)
    with pytest.raises(WatcherStartError) as info:
        w.start()
    assert info.value.reason == "unavailable"
    assert not w.is_alive()


def test_inotify_exhaustion_is_reported_as_such(tmp_path, monkeypatch):
    import errno

    import watchdog.observers

    class NoSpaceObserver:
        def __init__(self, **kw):
            pass

        def schedule(self, *a, **k):
            raise OSError(errno.ENOSPC, "inotify watch limit reached")

        def stop(self):
            pass

        def join(self, *a):
            pass

    monkeypatch.setattr(watchdog.observers, "Observer", NoSpaceObserver)
    w = SourceWatcher(str(tmp_path), IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[]), lambda c, r: None)
    with pytest.raises(WatcherStartError) as info:
        w.start()
    assert info.value.reason == "inotify_limit"


# ── the settle loop, stepped by hand ───────────────────────────────────────


def _event(etype: str, src: Path, dest: Path | None = None, is_dir: bool = False):
    return SimpleNamespace(event_type=etype, src_path=str(src), dest_path=str(dest) if dest else "", is_directory=is_dir)


def _stepper(tmp_path: Path, **kw):
    col = Collector()
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    w = SourceWatcher(str(tmp_path), m, col, debounce_s=kw.get("debounce_s", 0.05), stability_s=kw.get("stability_s", 0.05))
    w.BACKOFF_S = (0.05,)

    def run(until, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            w._tick()
            if until(col):
                return True
            time.sleep(0.02)
        return until(col)

    return w, col, run


def test_coalescing_latest_event_wins(tmp_path):
    w, col, run = _stepper(tmp_path)
    f = tmp_path / "doc.txt"
    f.write_text("x", encoding="utf-8")
    for _ in range(5):
        w._on_event(_event("modified", f))
    w._on_event(_event("deleted", f))
    w._on_event(_event("created", f))
    assert run(lambda c: c.calls)
    assert col.calls[0][1] == {"doc.txt"} and col.calls[0][2] == set()
    assert len(col.calls) == 1


def test_open_events_and_directory_modifications_are_noise(tmp_path):
    w, col, run = _stepper(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    w._on_event(_event("opened", tmp_path / "a.txt"))
    w._on_event(_event("closed_no_write", tmp_path / "a.txt"))
    w._on_event(_event("modified", tmp_path, is_dir=True))
    assert not w._pending


def test_unchanged_file_is_not_redelivered(tmp_path):
    w, col, run = _stepper(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    w._on_event(_event("modified", f))
    assert run(lambda c: c.calls)
    w._on_event(_event("modified", f))  # e.g. an access-time or attribute touch
    run(lambda c: not w._pending, timeout=1.0)
    assert len(col.calls) == 1


def test_a_vanished_file_becomes_a_removal(tmp_path):
    w, col, run = _stepper(tmp_path)
    w._on_event(_event("created", tmp_path / "ghost.txt"))
    assert run(lambda c: c.calls)
    assert col.calls[0][2] == {"ghost.txt"}


def test_a_removal_of_a_file_that_exists_is_settled_as_a_change(tmp_path):
    # The "created" that followed the "deleted" was lost: never drop a live file.
    w, col, run = _stepper(tmp_path)
    f = tmp_path / "still_here.txt"
    f.write_text("x", encoding="utf-8")
    w._on_event(_event("deleted", f))
    assert run(lambda c: c.calls)
    assert col.calls[0][1] == {"still_here.txt"} and col.calls[0][2] == set()


def test_a_locked_file_waits_then_is_delivered_anyway(tmp_path, monkeypatch):
    w, col, run = _stepper(tmp_path)
    w.MAX_ATTEMPTS = 3
    f = tmp_path / "open_in_word.docx"
    f.write_bytes(b"PK")
    checks = []
    monkeypatch.setattr(SourceWatcher, "_locked", staticmethod(lambda p, st: checks.append(p) or True))
    w._on_event(_event("modified", f))
    assert run(lambda c: c.calls)
    assert len(checks) == 3  # rechecked with backoff until MAX_ATTEMPTS
    assert col.calls[0][1] == {"open_in_word.docx"}


def test_events_inside_a_bundle_report_the_bundle(tmp_path):
    w, col, run = _stepper(tmp_path)
    inner = tmp_path / "Tool.app" / "Contents"
    inner.mkdir(parents=True)
    (inner / "Info.plist").write_text("x", encoding="utf-8")
    w._on_event(_event("modified", inner / "Info.plist"))
    assert run(lambda c: c.calls)
    assert col.calls[0][1] == {"Tool.app"}


def test_overflow_asks_for_a_full_rescan(tmp_path, monkeypatch):
    monkeypatch.setattr(wmod, "_MAX_PENDING", 3)
    w, col, run = _stepper(tmp_path)
    for i in range(10):
        w._on_event(_event("created", tmp_path / f"f{i}.txt"))
    assert run(lambda c: ROOT_RESCAN in c.changed)
    assert len(w._pending) <= 3


def test_events_outside_the_root_are_ignored(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    w, col, run = _stepper(root)
    w._on_event(_event("created", tmp_path / "elsewhere.txt"))
    w._on_event(_event("deleted", root, is_dir=True))  # the root itself
    assert not w._pending
