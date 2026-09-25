"""Bringing the index up to date before research reads it.

Two halves:

- the research step's decisions, with a scripted runtime: what it waits for,
  when it stops waiting, and what the dossier is told (the real engine under
  a real compile is in ``test_research_freshness_e2e``);
- the engine's side: comparing a file with the disk, queueing it first
  without restarting work in flight, and scan tickets.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.userdocs.discovery.hashing import changed_on_disk
from app.userdocs.research import freshness as F
from app.userdocs.research.context import ProgressSink, ResearchContext, ResearchLLM, ResearchSpec
from app.userdocs.research.types import Dossier
from app.userdocs.runtime import P_BULK, P_INTERACTIVE, ProfileRuntime

from .test_query_engine import Index
from .test_research_compile import _md


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(F, "POLL_S", 0.01)


# ── the research step, with a scripted runtime ──────────────────────────────


class FakeDB:
    """File statuses; each poll lets the "indexer" finish ``per_poll`` files."""

    def __init__(self, statuses: dict[int, str], per_poll: int = 1):
        self.statuses = dict(statuses)
        self.per_poll = per_poll

    def files_by_ids(self, ids):
        dirty = [i for i in ids if self.statuses.get(i) == "dirty"]
        for i in dirty[:self.per_poll]:
            self.statuses[i] = "indexed"
        return {i: {"id": i, "status": self.statuses[i], "source": "local"} for i in ids if i in self.statuses}


class FakeRuntime:
    def __init__(self, db: FakeDB, *, changes=(), poll=False, blocker=None, scan_after=1, settled_after=0):
        self.db = db
        self.changes = [list(c) for c in changes]    # what each queue_if_changed call finds
        self.poll = poll
        self.blocker = blocker
        self.scan_after = scan_after                 # scan_done() calls before the scan has run
        self.settled_after = settled_after
        self.tickets: list[str] = []
        self.queue_calls = 0
        self.in_flight: set[int] = set()

    def finds_new_files_by_scan(self):
        return self.poll

    def scan_blocker(self):
        return self.blocker

    def sync_blocker(self, source="local"):
        return self.blockers.get(source, self.blocker) if hasattr(self, "blockers") else self.blocker

    def scan_ticket(self, reason):
        self.tickets.append(reason)
        return 1

    def scan_done(self, ticket):
        self.scan_after -= 1
        return self.scan_after < 0

    def watch_settled(self):
        self.settled_after -= 1
        return self.settled_after < 0

    def indexing_any(self, ids):
        return bool(self.in_flight & set(ids))

    def queue_if_changed(self, rows):
        self.queue_calls += 1
        changed = self.changes.pop(0) if self.changes else []
        for i in changed:
            self.db.statuses[i] = "dirty"
        queued = [int(r["id"]) for r in rows if r.get("status") == "dirty" and int(r["id"]) not in changed]
        return changed, queued


def _ctx(rt) -> ResearchContext:
    engine = SimpleNamespace(db=rt.db if rt is not None else None, runtime=rt)
    return ResearchContext(
        profile="alice", job_id="j1", spec=ResearchSpec(question="q", mode="compile"), engine=engine,
        llm=ResearchLLM(SimpleNamespace(), budget=1000), dossier=Dossier(job_id="j1", mode="compile",
                                                                          domain="general", question="q", status="running"),
        progress_sink=ProgressSink(),
    )


def _rows(*ids, status="indexed"):
    return [{"id": i, "status": status, "source": "local"} for i in ids]


def _steps(ctx):
    return [(s["label"], s["status"]) for s in ctx.progress_sink.steps]


def test_without_a_runtime_nothing_happens():
    ctx = _ctx(None)
    assert asyncio.run(F.settle_discovery(ctx)) == []
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert not out.changed and out.notes == []


def test_changed_files_are_reindexed_first_then_checked_once_more():
    rt = FakeRuntime(FakeDB({1: "indexed", 2: "indexed", 3: "indexed"}), changes=[[1, 2]])
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, _rows(1, 2, 3)))
    assert out.changed and out.notes == []
    assert rt.db.statuses == {1: "indexed", 2: "indexed", 3: "indexed"}
    assert rt.queue_calls == 2, "the re-indexed files are compared with the disk again"
    assert _steps(ctx) == [("Re-indexing 2 files changed since the last sync", "done")]
    assert ctx.progress_sink.done == ctx.progress_sink.total == 2


def test_a_file_saved_again_while_indexing_goes_round_again():
    rt = FakeRuntime(FakeDB({1: "indexed"}), changes=[[1], [1]])
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert out.changed and out.notes == []
    assert [label for label, _ in _steps(ctx)] == ["Re-indexing 1 file changed since the last sync"] * 2


def test_files_already_queued_are_waited_for_too():
    rt = FakeRuntime(FakeDB({1: "dirty", 2: "indexed"}))
    out = asyncio.run(F.refresh_files(_ctx(rt), _rows(2) + _rows(1, status="dirty")))
    assert out.changed and rt.db.statuses[1] == "indexed"


def test_nothing_changed_means_no_step_and_no_new_scope():
    rt = FakeRuntime(FakeDB({1: "indexed"}))
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert not out.changed and out.notes == [] and _steps(ctx) == []


def test_a_stalled_queue_is_not_waited_for_forever(monkeypatch):
    monkeypatch.setattr(F, "STALL_S", 0.1)
    rt = FakeRuntime(FakeDB({1: "indexed"}, per_poll=0), changes=[[1]])
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert out.changed, "the scope is resolved again: the file now reads as not indexed yet"
    assert out.notes == ["1 file in scope changed since it was last indexed and was not re-indexed in time, "
                         "so it is listed as not indexed yet."]
    assert _steps(ctx)[0][1] == "failed"


def test_a_file_being_indexed_is_not_a_stall(monkeypatch):
    monkeypatch.setattr(F, "STALL_S", 0.05)
    db = FakeDB({1: "indexed"}, per_poll=0)
    rt = FakeRuntime(db, changes=[[1]])
    rt.in_flight = {1}
    polls = 0
    real = db.files_by_ids

    def slow(ids):
        # A long extraction: in flight for a while, then done.
        nonlocal polls
        polls += 1
        if polls > 20:
            db.per_poll = 1
        return real(ids)

    db.files_by_ids = slow
    out = asyncio.run(F.refresh_files(_ctx(rt), _rows(1)))
    assert out.notes == [] and db.statuses[1] == "indexed"


def test_with_sync_paused_files_are_queued_but_not_waited_for():
    rt = FakeRuntime(FakeDB({1: "indexed"}, per_poll=0), changes=[[1]], blocker=("paused", "user"))
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert out.changed and rt.db.statuses[1] == "dirty"
    assert out.notes == ["1 file in scope changed since it was last indexed, but sync is paused (user), "
                         "so it is listed as not indexed yet."]
    assert _steps(ctx) == []


def test_polling_mode_scans_before_the_scope_is_resolved():
    rt = FakeRuntime(FakeDB({}), poll=True, scan_after=3)
    ctx = _ctx(rt)
    assert asyncio.run(F.settle_discovery(ctx)) == []
    assert rt.tickets == ["research"]
    assert _steps(ctx) == [("Checking the folder for new and changed files", "done")]


def test_a_scan_that_does_not_finish_in_time_is_reported(monkeypatch):
    monkeypatch.setattr(F, "SCAN_WAIT_S", 0.05)
    rt = FakeRuntime(FakeDB({}), poll=True, scan_after=10**9)
    ctx = _ctx(rt)
    notes = asyncio.run(F.settle_discovery(ctx))
    assert notes == ["The folder check did not finish in time: files added in the last few minutes may be missing."]
    assert _steps(ctx)[0][1] == "failed"


def test_no_scan_is_asked_for_while_scans_are_held():
    rt = FakeRuntime(FakeDB({}), poll=True, blocker=("awaiting_confirmation", "mass_delete"))
    notes = asyncio.run(F.settle_discovery(_ctx(rt)))
    assert rt.tickets == []
    assert notes and "sync is waiting for a confirmation (mass_delete)" in notes[0]


def test_a_native_watcher_is_waited_for_until_it_has_handed_everything_over(monkeypatch):
    rt = FakeRuntime(FakeDB({}), settled_after=3)
    assert asyncio.run(F.settle_discovery(_ctx(rt))) == []
    assert rt.tickets == [], "a live watcher finds new files itself: no scan"

    monkeypatch.setattr(F, "WATCH_SETTLE_S", 0.05)
    rt = FakeRuntime(FakeDB({}), settled_after=10**9)
    notes = asyncio.run(F.settle_discovery(_ctx(rt)))
    assert notes and "still settling" in notes[0]


def test_waits_leave_the_job_its_reading_time():
    rt = FakeRuntime(FakeDB({1: "indexed"}, per_poll=0), changes=[[1]])
    ctx = _ctx(rt)
    ctx.deadline = time.monotonic() + F.RESERVE_S  # nothing left to wait with
    out = asyncio.run(F.refresh_files(ctx, _rows(1)))
    assert "not re-indexed in time" in out.notes[0]


# ── Google Drive ─────────────────────────────────────────────────────────────


class FakeDriveSource:
    enabled = True

    def __init__(self, *, blocker=None, after=None, sync_after=1):
        self.blocker = blocker          # sync_blocker(): a requested sync would not run
        self.after = after              # work_blocker() once the sync ran (e.g. it ended in a hold)
        self.sync_after = sync_after
        self.tickets: list[str] = []

    def sync_ticket(self, reason):
        self.tickets.append(reason)
        return 1

    def sync_done(self, ticket):
        self.sync_after -= 1
        return self.sync_after < 0

    def sync_blocker(self):
        return self.blocker

    def work_blocker(self):
        return self.after


def _drive_rt(*, local_on=True, **drive_kw) -> FakeRuntime:
    rt = FakeRuntime(FakeDB({}), poll=True)
    rt.local_on = lambda: local_on
    rt.drive = FakeDriveSource(**drive_kw)
    return rt


def test_drive_is_asked_for_its_changes_and_a_drive_only_profile_is_not_scanned():
    rt = _drive_rt(local_on=False, sync_after=3)
    ctx = _ctx(rt)
    assert asyncio.run(F.settle_discovery(ctx)) == []
    assert rt.tickets == [] and rt.drive.tickets == ["research"]
    assert _steps(ctx) == [("Checking Google Drive for changes", "done")]


def test_a_local_only_scope_leaves_drive_alone_and_a_drive_only_one_the_folder():
    rt = _drive_rt()
    ctx = _ctx(rt)
    ctx.spec.scope = {"folder": ["MKT-report"], "source": "local"}
    asyncio.run(F.settle_discovery(ctx))
    assert rt.tickets == ["research"] and rt.drive.tickets == []

    rt = _drive_rt()
    ctx = _ctx(rt)
    ctx.spec.scope = {"source": "drive"}
    asyncio.run(F.settle_discovery(ctx))
    assert rt.tickets == [] and rt.drive.tickets == ["research"]


def test_an_analyze_job_without_a_reference_scope_checks_both():
    rt = _drive_rt()
    ctx = _ctx(rt)
    ctx.spec.mode, ctx.spec.scope, ctx.spec.reference_scope = "analyze", {"source": "local"}, None
    asyncio.run(F.settle_discovery(ctx))
    assert rt.tickets == ["research"] and rt.drive.tickets == ["research"]


def test_a_drive_sync_that_ends_in_a_hold_is_reported():
    rt = _drive_rt(local_on=False, after=("hold", "drive_unreachable"))
    ctx = _ctx(rt)
    notes = asyncio.run(F.settle_discovery(ctx))
    assert notes == ["Google Drive could not be checked for changes (Google Drive sync is on hold "
                     "(drive_unreachable)): Drive files added or edited since the last sync may be missing "
                     "or outdated."]
    assert _steps(ctx) == [("Checking Google Drive for changes", "failed")]


def test_no_drive_sync_is_asked_for_while_drive_waits_for_a_decision():
    rt = _drive_rt(local_on=False, blocker=("awaiting_confirmation", "mass_delete"))
    notes = asyncio.run(F.settle_discovery(_ctx(rt)))
    assert rt.drive.tickets == []
    assert "Google Drive sync is waiting for a confirmation (mass_delete)" in notes[0]


def test_each_source_is_waited_for_only_while_its_sync_runs():
    db = FakeDB({1: "indexed", 2: "dirty"})
    rt = FakeRuntime(db, changes=[[1]])
    rt.blockers = {"local": None, "drive": ("hold", "drive_unreachable")}
    rows = [{"id": 1, "status": "indexed", "source": "local"}, {"id": 2, "status": "dirty", "source": "drive"}]
    ctx = _ctx(rt)
    out = asyncio.run(F.refresh_files(ctx, rows))
    assert db.statuses == {1: "indexed", 2: "dirty"}, "the local file was waited for; the held Drive one was not"
    assert out.changed and out.notes == [
        "1 Google Drive file in scope changed since it was last indexed, but Google Drive sync is on hold "
        "(drive_unreachable), so it is listed as not indexed yet."]
    assert _steps(ctx)[0] == ("Re-indexing 1 file changed since the last sync", "done")


# ── the engine's side ────────────────────────────────────────────────────────


def _match_disk(ix: Index, row: dict, path: Path) -> dict:
    st = path.stat()
    ix.db.update_file(int(row["id"]), size=st.st_size, mtime_ns=st.st_mtime_ns)
    return ix.db.get_file(int(row["id"]))


def test_changed_on_disk_compares_size_and_mtime_only(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "empty.txt").write_bytes(b"")
    st = (root / "empty.txt").stat()
    row = {"rel_path": "empty.txt", "size": 0, "mtime_ns": st.st_mtime_ns}
    assert changed_on_disk(str(root), row) is False, "an empty file is not always stale"
    assert changed_on_disk(str(root), {**row, "mtime_ns": st.st_mtime_ns + 1}) is True
    assert changed_on_disk(str(root), {**row, "size": 5}) is True
    assert changed_on_disk(str(root), {**row, "rel_path": "gone.txt"}) is True
    assert changed_on_disk(str(root), {"rel_path": "empty.txt"}) is True


def test_prioritize_moves_queued_files_up_without_requeueing(tmp_path):
    ix = Index(tmp_path)
    a = ix.add("a.md", _md("A", "alpha"))
    b = ix.add("b.md", _md("B", "beta"))
    ix.db.mark_dirty([a["id"]], priority=P_BULK)
    before = ix.db.get_file(a["id"])
    assert ix.db.prioritize([a["id"], b["id"]], priority=P_INTERACTIVE) == 1, "only queued files move"
    after = ix.db.get_file(a["id"])
    assert after["priority"] == P_INTERACTIVE and after["queued_at"] == before["queued_at"]
    assert ix.db.get_file(b["id"])["status"] == "indexed"
    assert ix.db.prioritize([a["id"]], priority=P_BULK) == 0, "never moved down"
    ix.db.close()


def _runtime(ix: Index, root: Path) -> ProfileRuntime:
    rt = ProfileRuntime(SimpleNamespace(wake=lambda: None), "alice", "u1")
    rt.db, rt.root = ix.db, str(root)
    return rt


def test_queued_drive_files_are_moved_up_even_with_no_local_folder(tmp_path):
    # Drive's change feed queued it; research only moves it up. With Drive
    # alone on, there is no folder to compare local rows with.
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    drive_row = ix.db.insert_file("drive", "Drive/plan.md", "k-plan", name="plan.md", name_folded="plan.md",
                                  ext=".md", status="dirty", priority=P_BULK)
    local = ix.add("notes.md", _md("N", "n"))
    rt = _runtime(ix, tmp_path)
    rt.root = None
    changed, waiting = rt.queue_if_changed([ix.db.get_file(drive_row["id"]), local])
    assert changed == [] and waiting == [drive_row["id"]]
    assert ix.db.get_file(drive_row["id"])["priority"] == P_INTERACTIVE
    assert ix.db.get_file(local["id"])["status"] == "indexed"
    ix.db.close()


def test_queue_if_changed_queues_only_what_differs_on_disk(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "same.md").write_text("same", encoding="utf-8")
    (root / "edited.md").write_text("edited now", encoding="utf-8")
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    same = _match_disk(ix, ix.add("same.md", _md("S", "s")), root / "same.md")
    edited = ix.add("edited.md", _md("E", "e"))            # indexed at another size and time
    gone = ix.add("gone.md", _md("G", "g"))                # no longer on disk
    queued = _match_disk(ix, ix.add("queued.md", _md("Q", "q")), root / "same.md")
    ix.db.mark_dirty([queued["id"]], priority=P_BULK)
    queued = ix.db.get_file(queued["id"])
    drive = {**same, "id": 999, "source": "drive"}
    rt = _runtime(ix, root)

    changed, waiting = rt.queue_if_changed([same, edited, gone, queued, drive])
    assert sorted(changed) == sorted([edited["id"], gone["id"]])
    assert waiting == [queued["id"]]
    for fid in changed + waiting:
        row = ix.db.get_file(fid)
        assert row["status"] == "dirty" and row["priority"] == P_INTERACTIVE
    assert ix.db.get_file(queued["id"])["queued_at"] == queued["queued_at"], "work in flight is not restarted"
    assert ix.db.get_file(same["id"])["status"] == "indexed"
    ix.db.close()


def test_a_scan_already_running_does_not_answer_a_new_ticket(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    rt = _runtime(ix, tmp_path)
    rt.scanning, rt.scans_started, rt.scans_finished = True, 3, 2
    ticket = rt.scan_ticket("research")
    assert ticket == 4 and rt.scan_requested == "research"
    rt.scans_finished = 3                       # the scan that was running when asked
    assert not rt.scan_done(ticket)
    rt.scans_started = rt.scans_finished = 4    # the next one
    assert rt.scan_done(ticket)
    ix.db.close()


def test_sync_and_scan_blockers_follow_what_the_workers_check(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    rt = _runtime(ix, tmp_path)
    rt.active = True
    assert rt.sync_blocker() is None and rt.scan_blocker() is None
    rt.confirmation = {"kind": "mass_delete"}
    assert rt.sync_blocker() is None, "held deletions do not stop indexing"
    assert rt.scan_blocker() == ("awaiting_confirmation", "mass_delete"), "they do stop scans"
    rt.confirmation, rt.paused_user = None, True
    assert rt.sync_blocker() == ("paused", "user") and rt.scan_blocker() == ("paused", "user")
    ix.db.close()


def test_no_live_watcher_means_new_files_are_found_by_scanning(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    rt = _runtime(ix, tmp_path)
    rt.watch_mode, rt.watcher = "poll", None
    assert rt.finds_new_files_by_scan()
    rt.watch_mode = "native"
    rt.watcher = SimpleNamespace(is_alive=lambda: False, pending_count=lambda: 0)
    assert rt.finds_new_files_by_scan(), "a dead watcher hears nothing"
    rt.watcher = SimpleNamespace(is_alive=lambda: True, pending_count=lambda: 2)
    assert not rt.finds_new_files_by_scan()
    assert not rt.watch_settled()
    rt.watcher = SimpleNamespace(is_alive=lambda: True, pending_count=lambda: 0)
    assert rt.watch_settled()
    rt.on_watch_paths({"a.md"}, set())
    assert not rt.watch_settled(), "handed over, not yet in the index"
    ix.db.close()


def test_research_finds_the_runtime_on_the_query_engine():
    # open_engine hands research the profile's runtime; without it (tests,
    # read-only uses) the freshness step is skipped.
    from app.userdocs.query.engine import QueryEngine

    assert QueryEngine("alice", db=None).runtime is None
    rt = object()
    assert QueryEngine("alice", db=None, runtime=rt).runtime is rt
