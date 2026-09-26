"""Moving an older install's document trees to where this build keeps them.

``app.documents.relocate`` runs at boot (and after a restore) before anything
opens these files. What must hold, pinned here against real directories:

- each profile's Cremind manual pages go from ``<SYS>/<name>/documents`` to
  ``storage/cremind_documents/profiles/<uuid>`` — by uuid, never across
  profiles, and never confused by a profile NAMED ``storage`` (whose pages sat
  exactly where the indexes now live), ``documents`` (which owned the old
  shared mirror) or ``cremind_documents``;
- each Documentation search index goes from ``storage/userdocs/<uid>`` to
  ``storage/documents/<uid>`` whole and intact — same file, same identity,
  same epoch, so nothing is re-extracted or re-captioned;
- a destination that already differs is a conflict: both sides kept, an
  actionable error journaled and reported, nothing overwritten;
- an interrupted run resumes; a cross-device move copies and verifies;
- the installation lock takes over a dead holder but waits for a live one.
"""

from __future__ import annotations

import errno
import json
import os
import socket
from pathlib import Path

import pytest

from app.documents import relocate
from app.documents.index import IndexDB

ALICE, BOB = "a1a1a1a1-0000-4000-8000-000000000001", "b2b2b2b2-0000-4000-8000-000000000002"


def _page(path: Path, text: str = "---\ndescription: \"x\"\n---\n\nbody\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _authored(base: Path, uid: str) -> Path:
    return base / "storage" / "cremind_documents" / "profiles" / uid


def _make_index(base: Path, uid: str, *, root: str = "userdocs", owner: str | None = None) -> tuple[Path, str]:
    """A real index DB at ``storage/<root>/<uid>`` owned by ``owner`` (default
    ``uid``), with a row in it and a rebuild backup beside it."""
    d = base / "storage" / root / uid
    db = IndexDB.open(str(d / "index.db"), profile_uid=owner or uid)
    epoch = db.epoch
    db.set_meta("marker", f"data of {uid}")
    db.close()
    (d / "index.db.bak").write_bytes(b"old backup " + uid.encode())
    return d, epoch


def _open(base: Path, uid: str) -> IndexDB:
    return IndexDB.open(str(base / "storage" / "documents" / uid / "index.db"), profile_uid=uid)


# ── manual pages ────────────────────────────────────────────────────────────


def test_two_profiles_pages_move_to_their_own_uuid_directories(tmp_path):
    _page(tmp_path / "alice" / "documents" / "notes.md", "alice notes")
    _page(tmp_path / "alice" / "documents" / "sub" / "deep.md", "alice deep")
    _page(tmp_path / "bob" / "documents" / "notes.md", "bob notes")
    _page(tmp_path / "alice" / "PERSONA.md", "alice persona")

    report = relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])

    assert report.ran and not report.errors
    assert (_authored(tmp_path, ALICE) / "notes.md").read_text(encoding="utf-8") == "alice notes"
    assert (_authored(tmp_path, ALICE) / "sub" / "deep.md").read_text(encoding="utf-8") == "alice deep"
    assert (_authored(tmp_path, BOB) / "notes.md").read_text(encoding="utf-8") == "bob notes"
    # No cross-profile movement, and the rest of each tree is untouched.
    assert not (_authored(tmp_path, BOB) / "sub").exists()
    assert (tmp_path / "alice" / "PERSONA.md").exists()
    assert not (tmp_path / "alice" / "documents").exists()
    assert report.moved_authored_files == 3


def test_a_profile_without_a_row_is_left_alone(tmp_path):
    """Only known profile rows map a name to a uuid; a leftover tree of a
    deleted profile is kept where it is (its data was never removed)."""
    _page(tmp_path / "ghost" / "documents" / "notes.md")
    relocate.relocate(tmp_path, [("alice", ALICE)])
    assert (tmp_path / "ghost" / "documents" / "notes.md").exists()


def test_a_profile_named_storage_moves_its_pages_but_not_the_indexes(tmp_path):
    """``<SYS>/storage/documents`` held a profile named ``storage``'s pages
    before the rename — and is where the indexes live now. Pages go out,
    index directories stay."""
    _page(tmp_path / "storage" / "documents" / "mine.md", "storage's page")
    _page(tmp_path / "storage" / "documents" / "guides" / "g.md", "storage's guide")
    # An index already at the new place (a second boot, or a fresh index).
    _make_index(tmp_path, ALICE, root="documents")
    _make_index(tmp_path, BOB)  # still at the legacy root

    report = relocate.relocate(tmp_path, [("storage", "5707a6e0-uid-storage"), ("alice", ALICE), ("bob", BOB)])

    assert not report.errors, report.errors
    moved = _authored(tmp_path, "5707a6e0-uid-storage")
    assert (moved / "mine.md").read_text(encoding="utf-8") == "storage's page"
    assert (moved / "guides" / "g.md").exists()
    assert not (moved / ALICE).exists() and not (moved / BOB).exists()
    assert (tmp_path / "storage" / "documents" / ALICE / "index.db").exists()
    assert (tmp_path / "storage" / "documents" / BOB / "index.db").exists()
    assert not (tmp_path / "storage" / "documents" / "mine.md").exists()
    assert not (tmp_path / "storage" / "documents" / "guides").exists()


def test_profiles_named_documents_and_cremind_documents(tmp_path):
    _page(tmp_path / "documents" / "documents" / "a.md", "documents' page")
    _page(tmp_path / "documents" / "PERSONA.md", "persona")
    _page(tmp_path / "cremind_documents" / "documents" / "b.md", "cd page")

    report = relocate.relocate(
        tmp_path, [("documents", "d0c5-uid"), ("cremind_documents", "cd00-uid")],
    )

    assert not report.errors
    assert (_authored(tmp_path, "d0c5-uid") / "a.md").read_text(encoding="utf-8") == "documents' page"
    assert (_authored(tmp_path, "cd00-uid") / "b.md").read_text(encoding="utf-8") == "cd page"
    # Seeding happened; the old shared mirror is still the "documents"
    # profile's own tree, so it is kept.
    _page(tmp_path / "storage" / "cremind_documents" / "shared" / "document.md")
    relocate.retire_legacy_trees(tmp_path, ["documents", "cremind_documents"])
    assert (tmp_path / "documents" / "PERSONA.md").exists()
    assert relocate.Journal.load(tmp_path).step("shared_legacy")["state"] == "kept"


def test_the_old_shared_mirror_goes_only_after_seeding_and_only_if_no_profile_owns_it(tmp_path):
    _page(tmp_path / "documents" / "document.md", "old mirror")
    relocate.retire_legacy_trees(tmp_path, ["admin"])
    assert (tmp_path / "documents").exists(), "not before the new mirror exists"
    assert relocate.Journal.load(tmp_path).step("shared_legacy")["state"] == "pending"

    _page(tmp_path / "storage" / "cremind_documents" / "shared" / "document.md", "new mirror")
    removed = relocate.retire_legacy_trees(tmp_path, ["admin"])
    assert removed == ["documents"]
    assert not (tmp_path / "documents").exists()


def test_a_conflicting_page_is_kept_on_both_sides_and_reported(tmp_path):
    _page(tmp_path / "alice" / "documents" / "same.md", "identical")
    _page(tmp_path / "alice" / "documents" / "clash.md", "old version")
    _page(tmp_path / "alice" / "documents" / "fresh.md", "only old")
    _page(_authored(tmp_path, ALICE) / "same.md", "identical")
    _page(_authored(tmp_path, ALICE) / "clash.md", "new version")

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert [e["step"] for e in report.errors] == [f"authored:{ALICE}"]
    # Never overwritten, never lost.
    assert (_authored(tmp_path, ALICE) / "clash.md").read_text(encoding="utf-8") == "new version"
    assert (tmp_path / "alice" / "documents" / "clash.md").read_text(encoding="utf-8") == "old version"
    # The rest still moved; the identical duplicate was dropped from the old side.
    assert (_authored(tmp_path, ALICE) / "fresh.md").read_text(encoding="utf-8") == "only old"
    assert not (tmp_path / "alice" / "documents" / "same.md").exists()

    pending = relocate.pending_errors(tmp_path)
    assert len(pending) == 1 and pending[0]["state"] == "conflict"
    assert "clash.md" in pending[0]["error"] and "restart" in pending[0]["error"]
    assert pending[0]["conflicts"] == ["clash.md"]

    # The person resolves it; the next run records the step as done.
    (tmp_path / "alice" / "documents" / "clash.md").unlink()
    relocate.relocate(tmp_path, [("alice", ALICE)])
    assert relocate.pending_errors(tmp_path) == []
    assert not (tmp_path / "alice" / "documents").exists()


# ── indexes ─────────────────────────────────────────────────────────────────


def test_indexes_move_intact_so_nothing_is_rebuilt(tmp_path):
    _, epoch_a = _make_index(tmp_path, ALICE)
    _, epoch_b = _make_index(tmp_path, BOB)
    (tmp_path / "storage" / "userdocs" / "tmp" / "extract-1").mkdir(parents=True)
    (tmp_path / "storage" / "userdocs" / "tmp" / "extract-1" / "page.png").write_bytes(b"png")

    report = relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])

    assert not report.errors and report.moved_indexes == 2
    assert not (tmp_path / "storage" / "userdocs").exists(), "scratch dropped, legacy root removed"
    for uid, epoch in ((ALICE, epoch_a), (BOB, epoch_b)):
        d = tmp_path / "storage" / "documents" / uid
        assert (d / "index.db.bak").read_bytes() == b"old backup " + uid.encode()
        db = _open(tmp_path, uid)
        try:
            # Same file: same identity, same epoch (so the same collection
            # names), same content — IndexDB.open did not rebuild it.
            assert db.profile_uid == uid
            assert db.epoch == epoch
            assert db.get_meta("marker") == f"data of {uid}"
        finally:
            db.close()
    steps = relocate.Journal.load(tmp_path).steps()
    assert steps[f"index:{ALICE}"]["state"] == "done"
    assert "manifest" not in steps[f"index:{ALICE}"]


def test_an_index_claiming_another_profile_is_not_moved(tmp_path):
    _make_index(tmp_path, ALICE, owner=BOB)

    report = relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])

    assert [e["step"] for e in report.errors] == [f"index:{ALICE}"]
    assert (tmp_path / "storage" / "userdocs" / ALICE / "index.db").exists()
    assert not (tmp_path / "storage" / "documents" / ALICE).exists()
    assert "belongs to profile" in relocate.pending_errors(tmp_path)[0]["error"]


def test_a_differing_index_at_the_destination_is_a_conflict(tmp_path):
    _make_index(tmp_path, ALICE)
    _make_index(tmp_path, ALICE, root="documents")  # a fresh one, different epoch

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert [e["state"] for e in report.errors] == ["conflict"]
    assert (tmp_path / "storage" / "userdocs" / ALICE / "index.db").exists()
    assert (tmp_path / "storage" / "documents" / ALICE / "index.db").exists()


def test_an_identical_copy_at_the_destination_just_drops_the_old_one(tmp_path):
    import shutil

    src, _ = _make_index(tmp_path, ALICE)
    shutil.copytree(src, tmp_path / "storage" / "documents" / ALICE)

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert not report.errors
    assert not src.exists()


def test_an_interrupted_same_device_move_is_verified_and_closed(tmp_path):
    """The rename happened, the process died before the journal said so."""
    src, _ = _make_index(tmp_path, ALICE)
    journal = relocate.Journal.load(tmp_path)
    journal.record(
        f"index:{ALICE}", state="moving", kind="index", uid=ALICE,
        manifest=relocate._manifest(src),
    )
    dst = tmp_path / "storage" / "documents" / ALICE
    dst.parent.mkdir(parents=True)
    os.rename(src, dst)

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert not report.errors
    assert relocate.Journal.load(tmp_path).step(f"index:{ALICE}")["state"] == "done"


def test_a_cross_device_move_copies_verifies_and_resumes(tmp_path, monkeypatch):
    """Across devices: copy to a temporary sibling, verify, rename, remove.
    A crash while removing the source leaves a verified copy in place and a
    half-deleted source; the next run finishes the removal."""
    src, epoch = _make_index(tmp_path, ALICE)
    real_rename = os.rename

    def cross_device(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b)

    monkeypatch.setattr(relocate.os, "rename", cross_device)
    real_rmtree = relocate.shutil.rmtree
    calls = {"n": 0}

    def crash_on_source_removal(path, *a, **kw):
        if Path(path) == src and calls["n"] == 0:
            calls["n"] += 1
            (src / "index.db.bak").unlink()  # half-deleted...
            raise KeyboardInterrupt("power cut")  # ...then the process dies
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(relocate.shutil, "rmtree", crash_on_source_removal)
    with pytest.raises(KeyboardInterrupt):
        relocate.relocate(tmp_path, [("alice", ALICE)])
    # The installation lock of the dead run is ours by pid: the next run in
    # this process takes it over rather than waiting.
    report = relocate.relocate(tmp_path, [("alice", ALICE)], wait_s=0)

    assert report.ran and not report.errors, report.errors
    assert not src.exists()
    db = _open(tmp_path, ALICE)
    try:
        assert db.epoch == epoch and db.get_meta("marker") == f"data of {ALICE}"
    finally:
        db.close()
    assert not list((tmp_path / "storage" / "documents").glob(f"*{relocate.TMP_SUFFIX}"))


def test_an_interrupted_page_merge_resumes(tmp_path, monkeypatch):
    for i in range(4):
        _page(tmp_path / "alice" / "documents" / f"p{i}.md", f"page {i}")
    real_move = relocate._move_file
    moved = {"n": 0}

    def die_after_two(s, d):
        if moved["n"] == 2:
            raise KeyboardInterrupt("killed")
        moved["n"] += 1
        return real_move(s, d)

    monkeypatch.setattr(relocate, "_move_file", die_after_two)
    with pytest.raises(KeyboardInterrupt):
        relocate.relocate(tmp_path, [("alice", ALICE)])
    monkeypatch.setattr(relocate, "_move_file", real_move)

    report = relocate.relocate(tmp_path, [("alice", ALICE)], wait_s=0)

    assert not report.errors
    assert sorted(p.name for p in _authored(tmp_path, ALICE).iterdir()) == [f"p{i}.md" for i in range(4)]
    assert not (tmp_path / "alice" / "documents").exists()


def test_rerunning_is_a_no_op(tmp_path):
    _page(tmp_path / "alice" / "documents" / "a.md")
    _make_index(tmp_path, ALICE)
    relocate.relocate(tmp_path, [("alice", ALICE)])
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
                    and relocate.JOURNAL_FILE not in p.name)

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
                   and relocate.JOURNAL_FILE not in p.name)
    assert before == after
    assert report.moved_authored_files == 0 and report.moved_indexes == 0 and not report.errors


# ── an index whose move was skipped or failed ──────────────────────────────


def _foreign_lock(base: Path) -> Path:
    """A fresh lock of a pod on another host: live as far as this host can
    tell, so a run waits for it and then skips."""
    path = relocate.lock_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 7, "host": "old-pod-abc", "token": "x"}), encoding="utf-8")
    return path


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


def _write_through_index_path(uid: str, key: str, value: str) -> str:
    """What the engine does: resolve the index path, open it, write, close."""
    from app.documents.index import index_path

    path = index_path(uid)
    db = IndexDB.open(path, profile_uid=uid)
    try:
        db.set_meta(key, value)
    finally:
        db.close()
    return path


def test_a_skipped_move_uses_the_old_index_in_place_then_moves_it_whole(sysdir):
    """The boot relocation is skipped (another pod holds the lock), the
    engine starts anyway: it must work on the old index where it is — not
    create an empty one at the new place, which the next boot would record as
    a permanent conflict while everything re-extracted."""
    from app.documents.index import index_dir

    src, epoch = _make_index(sysdir, ALICE)
    lock = _foreign_lock(sysdir)
    report = relocate.relocate(sysdir, [("alice", ALICE)], wait_s=0.2)
    assert report.busy and not report.ran

    path = _write_through_index_path(ALICE, "written", "while unmoved")
    assert Path(path).parent == src, "the old directory is used in place"
    assert Path(index_dir(ALICE)) == src
    assert not (sysdir / "storage" / "documents" / ALICE).exists(), "no fresh index at the new place"

    lock.unlink()
    report = relocate.relocate(sysdir, [("alice", ALICE)])

    assert report.ran and not report.errors, report.errors
    assert report.moved_indexes == 1 and not src.exists()
    assert Path(index_dir(ALICE)) == sysdir / "storage" / "documents" / ALICE
    db = _open(sysdir, ALICE)
    try:
        # The same index, with what was written while it sat at the old place:
        # nothing to re-extract, the same epoch (so the same collections).
        assert db.epoch == epoch
        assert db.get_meta("marker") == f"data of {ALICE}"
        assert db.get_meta("written") == "while unmoved"
    finally:
        db.close()
    assert relocate.pending_errors(sysdir) == []


def test_a_failed_move_is_retried_next_boot_without_a_conflict(sysdir, monkeypatch):
    src, epoch = _make_index(sysdir, ALICE)

    def refuse(*_a, **_kw):
        raise PermissionError(13, "the file is in use")

    monkeypatch.setattr(relocate, "_move_dir", refuse)
    report = relocate.relocate(sysdir, [("alice", ALICE)])
    assert [e["state"] for e in report.errors] == ["error"]
    assert Path(_write_through_index_path(ALICE, "written", "after the failure")).parent == src
    monkeypatch.undo()
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))

    report = relocate.relocate(sysdir, [("alice", ALICE)])

    assert not report.errors and report.moved_indexes == 1
    assert relocate.pending_errors(sysdir) == []
    db = _open(sysdir, ALICE)
    try:
        assert db.epoch == epoch and db.get_meta("written") == "after the failure"
    finally:
        db.close()


def test_the_new_place_wins_once_it_holds_an_index(sysdir):
    """The fallback is only for an index that has not moved: when both places
    hold one (a conflict kept both), the current one is the one in use — as
    the conflict's message says."""
    from app.documents.index import index_dir, index_dirs

    _make_index(sysdir, ALICE)
    _make_index(sysdir, ALICE, root="documents")
    current, legacy = index_dirs(ALICE)
    assert index_dir(ALICE) == current
    assert Path(legacy).exists()
    # And with neither, a new index goes to the new place.
    assert Path(index_dir(BOB)) == sysdir / "storage" / "documents" / BOB


# ── problems that were resolved elsewhere ───────────────────────────────────


def test_a_conflict_whose_old_copy_was_deleted_is_closed(tmp_path):
    """The conflict message tells the person to delete the old folder; once
    they do, the step must stop being reported."""
    import shutil

    _make_index(tmp_path, ALICE)
    _make_index(tmp_path, ALICE, root="documents")
    assert [e["state"] for e in relocate.relocate(tmp_path, [("alice", ALICE)]).errors] == ["conflict"]
    assert len(relocate.pending_errors(tmp_path)) == 1

    shutil.rmtree(tmp_path / "storage" / "userdocs" / ALICE)
    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert not report.errors
    assert relocate.pending_errors(tmp_path) == []
    step = relocate.Journal.load(tmp_path).step(f"index:{ALICE}")
    assert step["state"] == "done" and step["resolved"] is True and "error" not in step
    assert (tmp_path / "storage" / "documents" / ALICE / "index.db").exists(), "the current one is untouched"


def test_an_error_whose_old_copy_was_deleted_is_closed(tmp_path):
    import shutil

    _make_index(tmp_path, ALICE, owner=BOB)  # "belongs to another profile": an error
    relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])
    assert [p["state"] for p in relocate.pending_errors(tmp_path)] == ["error"]

    shutil.rmtree(tmp_path / "storage" / "userdocs" / ALICE)
    relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])

    assert relocate.pending_errors(tmp_path) == []


def test_index_problems_name_their_profile_and_uuid(tmp_path):
    """The status route shows a profile only its own problems, by uuid — an
    index step knows only the uuid, so it carries the profile name too, and
    ``problem_uid`` reads the uuid off any step."""
    _make_index(tmp_path, ALICE)
    _make_index(tmp_path, ALICE, root="documents")
    _make_index(tmp_path, "0dead0-uid-of-a-deleted-profile", owner=BOB)
    relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])

    problems = {p["step"]: p for p in relocate.pending_errors(tmp_path)}
    alice = problems[f"index:{ALICE}"]
    assert alice["profile"] == "alice" and relocate.problem_uid(alice) == ALICE
    orphan = problems["index:0dead0-uid-of-a-deleted-profile"]
    assert "profile" not in orphan, "no row maps a deleted profile's uuid to a name"
    assert relocate.problem_uid(orphan) == "0dead0-uid-of-a-deleted-profile"
    assert relocate.problem_uid({"step": "indexes", "state": "error"}) is None


# ── deleting an index on purpose ────────────────────────────────────────────


def test_deleting_an_index_takes_the_old_copy_and_closes_its_step(tmp_path):
    """Settings → delete the local index (or deleting the profile) while a
    conflict kept a pre-rename copy: the copy goes too, and the next boot
    neither moves it back nor reports it."""
    import shutil

    _make_index(tmp_path, ALICE)
    _make_index(tmp_path, ALICE, root="documents")
    relocate.relocate(tmp_path, [("alice", ALICE)])
    assert relocate.pending_errors(tmp_path)

    shutil.rmtree(tmp_path / "storage" / "documents" / ALICE)  # what the purge removes itself
    assert relocate.forget_index(tmp_path, ALICE) is True

    assert not (tmp_path / "storage" / "userdocs" / ALICE).exists()
    assert relocate.pending_errors(tmp_path) == []
    report = relocate.relocate(tmp_path, [("alice", ALICE)])
    assert report.moved_indexes == 0 and not report.errors
    assert not (tmp_path / "storage" / "documents" / ALICE).exists(), "nothing resurrected"


def test_forget_profile_removes_both_places_and_leaves_other_profiles_alone(sysdir, monkeypatch):
    from app.documents import service as svc_module

    monkeypatch.setattr(svc_module, "_service", None)
    monkeypatch.setattr(svc_module, "_purge_research", lambda profile: None)
    _make_index(sysdir, ALICE)                     # the copy a conflict kept
    _make_index(sysdir, ALICE, root="documents")   # the one in use
    _make_index(sysdir, BOB)                       # another profile, not yet moved
    relocate.relocate(sysdir, [("alice", ALICE), ("bob", BOB)])
    assert [p["step"] for p in relocate.pending_errors(sysdir)] == [f"index:{ALICE}"]

    svc_module.forget_profile("alice", ALICE)

    assert not (sysdir / "storage" / "documents" / ALICE).exists()
    assert not (sysdir / "storage" / "userdocs" / ALICE).exists()
    assert relocate.pending_errors(sysdir) == []
    assert (sysdir / "storage" / "documents" / BOB / "index.db").exists()


# ── manual pages move once ──────────────────────────────────────────────────


CAROL = "c3c3c3c3-0000-4000-8000-000000000003"


def test_a_later_profile_never_inherits_an_earlier_namesakes_pages(tmp_path):
    """``bob`` was deleted before the upgrade; his old folder stayed behind.
    A new, unrelated ``bob`` created after the first relocation must not get
    those pages moved into its manual on its first restart."""
    _page(tmp_path / "alice" / "documents" / "a.md", "alice's page")
    _page(tmp_path / "bob" / "documents" / "secret.md", "the OLD bob's page")
    relocate.relocate(tmp_path, [("alice", ALICE)])  # the upgrade: bob has no row
    assert (_authored(tmp_path, ALICE) / "a.md").exists()

    report = relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)])  # the new bob's first boot

    assert not report.errors and report.moved_authored_files == 0
    assert not _authored(tmp_path, BOB).exists()
    assert (tmp_path / "bob" / "documents" / "secret.md").read_text(encoding="utf-8") == "the OLD bob's page"
    step = relocate.Journal.load(tmp_path).step(f"authored:{BOB}")
    assert step["state"] == "kept" and "left where it is" in step["warning"]
    assert [k["step"] for k in report.kept] == [f"authored:{BOB}"]
    assert relocate.pending_errors(tmp_path) == [], "a warning, not a problem to resolve"
    # Recorded once, not on every boot.
    assert relocate.relocate(tmp_path, [("alice", ALICE), ("bob", BOB)]).kept == []


def test_a_finished_profile_is_not_swept_again(tmp_path):
    """After the move, ``<SYS>/<name>/documents`` is an ordinary folder of
    the profile's own tree; files the profile keeps there later stay put."""
    _page(tmp_path / "alice" / "documents" / "a.md", "alice's page")
    relocate.relocate(tmp_path, [("alice", ALICE), ("carol", CAROL)])  # carol: nothing to move

    _page(tmp_path / "alice" / "documents" / "report.md", "a file alice's agent saved")
    _page(tmp_path / "carol" / "documents" / "notes.md", "carol's own folder")
    report = relocate.relocate(tmp_path, [("alice", ALICE), ("carol", CAROL)])

    assert report.moved_authored_files == 0 and not report.kept
    assert (tmp_path / "alice" / "documents" / "report.md").exists()
    assert (tmp_path / "carol" / "documents" / "notes.md").exists()
    assert not (_authored(tmp_path, ALICE) / "report.md").exists()


def test_an_unfinished_first_run_still_finishes(tmp_path, monkeypatch):
    """The first run died mid-move: the next one still moves every profile
    that existed then — the baseline, not the crash, decides."""
    for i in range(2):
        _page(tmp_path / "alice" / "documents" / f"p{i}.md", f"alice {i}")
    _page(tmp_path / "carol" / "documents" / "c.md", "carol")

    def die(*_a, **_kw):
        raise KeyboardInterrupt("killed")

    monkeypatch.setattr(relocate, "_relocate_authored", die)
    with pytest.raises(KeyboardInterrupt):
        relocate.relocate(tmp_path, [("alice", ALICE), ("carol", CAROL)])
    monkeypatch.undo()

    report = relocate.relocate(tmp_path, [("alice", ALICE), ("carol", CAROL)], wait_s=0)

    assert not report.errors and report.moved_authored_files == 3
    assert relocate.Journal.load(tmp_path).step(relocate.AUTHORED_BASELINE_STEP)["state"] == "done"


def test_a_restore_moves_the_restored_profiles_pages_whatever_ran_before(tmp_path):
    """An archive from before the move puts each restored profile's pages at
    ``<name>/documents`` again — owned by exactly the restored rows, even
    ones this install's first run never saw."""
    relocate.relocate(tmp_path, [("admin", ALICE)])  # this install's own first boot
    _page(tmp_path / "admin" / "documents" / "a.md", "restored admin page")
    _page(tmp_path / "bob" / "documents" / "b.md", "restored bob page")

    report = relocate.run_after_restore(tmp_path, [("admin", ALICE), ("bob", BOB)])

    assert not report.errors and report.moved_authored_files == 2
    assert (_authored(tmp_path, ALICE) / "a.md").exists()
    assert (_authored(tmp_path, BOB) / "b.md").exists()
    baseline = relocate.Journal.load(tmp_path).step(relocate.AUTHORED_BASELINE_STEP)
    assert set(baseline["profiles"]) == {ALICE, BOB}


# ── lock and journal ────────────────────────────────────────────────────────


def test_a_live_holder_makes_the_run_wait_then_skip(tmp_path):
    held = relocate.InstallationLock(tmp_path)
    assert held.acquire()
    try:
        # Another process on this host, alive: same pid is ours, so fake a
        # live foreign one — the parent of this test process.
        data = json.loads(held.path.read_text(encoding="utf-8"))
        data["pid"] = os.getppid()
        held.path.write_text(json.dumps(data), encoding="utf-8")
        report = relocate.relocate(tmp_path, [("alice", ALICE)], wait_s=0.3)
        assert report.busy and not report.ran
    finally:
        held.path.unlink(missing_ok=True)


def test_a_dead_holders_lock_is_taken_over(tmp_path):
    path = relocate.lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": socket.gethostname(), "token": "x"}),
                    encoding="utf-8")
    _page(tmp_path / "alice" / "documents" / "a.md")

    report = relocate.relocate(tmp_path, [("alice", ALICE)], wait_s=0)

    assert report.ran
    assert not path.exists(), "released after the run"


def test_a_silent_lock_from_another_host_goes_stale_by_age(tmp_path):
    path = relocate.lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 1, "host": "elsewhere", "token": "x"}), encoding="utf-8")
    lock = relocate.InstallationLock(tmp_path, wait_s=0, stale_after_s=3600)
    assert not lock.acquire(), "fresh: another host may still be working"
    old = path.stat().st_mtime - 7200
    os.utime(path, (old, old))
    assert lock.acquire()
    lock.release()


def test_a_damaged_journal_is_set_aside_not_fatal(tmp_path):
    path = relocate.journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    _page(tmp_path / "alice" / "documents" / "a.md")

    report = relocate.relocate(tmp_path, [("alice", ALICE)])

    assert report.ran and not report.errors
    assert json.loads(path.read_text(encoding="utf-8"))["steps"]
    assert list(path.parent.glob(f"{relocate.JOURNAL_FILE}.corrupt-*"))


# ── the pre-rename manual collection ────────────────────────────────────────


class _Store:
    def __init__(self, names, counts=None, *, down=False):
        self.names = set(names)
        self.counts = counts or {}
        self.down = down
        self.dropped: list[str] = []

    def list_collections(self):
        if self.down:
            raise ConnectionError("unreachable")
        return sorted(self.names)

    def count(self, name):
        return self.counts.get(name, 0)

    def delete_collection(self, name):
        self.dropped.append(name)
        self.names.discard(name)


def test_the_legacy_manual_collection_goes_only_once_its_replacement_holds_points(tmp_path):
    assert relocate.retire_legacy_manual_collection(None, system_dir=tmp_path) == "pending"
    down = _Store(["documentation_search"], down=True)
    assert relocate.retire_legacy_manual_collection(down, system_dir=tmp_path) == "pending"

    store = _Store(["documentation_search", "cremind_documentation_search"])
    assert relocate.retire_legacy_manual_collection(store, system_dir=tmp_path) == "pending"
    assert store.dropped == [], "the replacement is still empty"

    store.counts["cremind_documentation_search"] = 12
    assert relocate.retire_legacy_manual_collection(store, system_dir=tmp_path) == "done"
    assert store.dropped == ["documentation_search"]
    assert relocate.Journal.load(tmp_path).step("manual_collection")["state"] == "done"
    # Idempotent: nothing left to drop.
    assert relocate.retire_legacy_manual_collection(store, system_dir=tmp_path) == "done"
    assert store.dropped == ["documentation_search"]


# ── boot order ──────────────────────────────────────────────────────────────


def test_boot_relocates_before_any_document_service_starts():
    """Pinned as text (boot is one coroutine nested in ``main()``): the
    relocation must run before the manual service and its watchers (6b), the
    Documentation search engine with its workers and GC (7h) and research
    recovery (12b) — nothing may hold a file open while its directory moves."""
    pytest.importorskip("a2a")
    from app import server

    src = Path(server.__file__).read_text(encoding="utf-8")
    start = src.index("async def boot_storage_and_post_storage")
    block = src[start:src.index("\ndef _build_spa_components", start)]
    moved = block.index("doc_relocate.run_at_boot")
    assert moved < block.index("CremindDocumentSyncService(")
    assert moved < block.index("start_service()")
    assert moved < block.index("research_jobs.boot_recover()")
    assert block.index("seed_shared_from_app(") < block.index("retire_legacy_trees(")
    assert block.index("conversation_storage.initialize()") < moved
