"""The index relocation and the running engine, together.

``app.documents.relocate`` moves ``storage/userdocs/<uid>`` to
``storage/documents/<uid>`` at boot, before the engine starts. When that move
is skipped (another process held the lock) or fails, the engine starts all
the same — and must keep using the old index where it is instead of creating
a fresh, empty one at the new place: the next boot would then find two
different indexes, keep both as a conflict, and the profile would re-extract
everything. Deleting an index must take any pre-rename copy with it, or the
next boot's relocation moves the deleted index back.

The engine is the real one (see :mod:`test_engine_e2e`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("a2a")
pytest.importorskip("qdrant_client")

from app.documents import relocate  # noqa: E402
from app.documents import service as svc_module  # noqa: E402
from app.documents import state as uds_state  # noqa: E402

from .test_engine_e2e import _idle, _para, _start, _wait, env  # noqa: E402,F401

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

UID = "uid-alice"


def _dirs() -> tuple[Path, Path]:
    """(current, pre-rename) index directories of alice."""
    sysdir = Path(svc_module.uds.system_dir())
    return sysdir / "storage" / "documents" / UID, sysdir / "storage" / "userdocs" / UID


def _second_engine(env, monkeypatch) -> svc_module.DocumentsService:
    """A fresh engine, as after a restart, counting what it extracts."""
    svc = svc_module.DocumentsService()
    monkeypatch.setattr(svc_module, "_service", svc)
    real_extract = svc.extract

    def spy(req, *, size):
        env.extracted.append(req.name)
        return real_extract(req, size=size)

    svc.extract = spy
    return svc


def test_an_unmoved_index_is_used_in_place_and_moved_whole_next_boot(env, monkeypatch):
    (env.alice / "notes.md").write_text("# Notes\n\n" + "\n\n".join(_para(i) for i in range(5)),
                                        encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")
    epoch = rt.db.epoch
    indexed = rt.db.count_by_status("local").get("indexed")
    env.svc.stop(budget_s=3.0)
    current, legacy = _dirs()
    # An install from before the rename whose boot relocation was skipped.
    legacy.parent.mkdir(parents=True, exist_ok=True)
    os.rename(current, legacy)
    extracted_before = len(env.extracted)

    svc = _second_engine(env, monkeypatch)
    try:
        svc.start()
        rt = _wait(lambda: (r := svc.runtime("alice")) is not None and r.active and r.db is not None and r)
        # The old index, in place: same epoch, same rows, nothing re-read.
        assert Path(rt.db._path).parent == legacy
        assert rt.db.epoch == epoch
        assert rt.db.count_by_status("local").get("indexed") == indexed
        assert not (current / "index.db").exists(), "no fresh index at the new place"
    finally:
        svc.stop(budget_s=3.0)
    assert len(env.extracted) == extracted_before

    report = relocate.relocate(svc_module.uds.system_dir(), [("alice", UID), ("bob", "uid-bob")])

    assert not report.errors, report.errors
    assert report.moved_indexes == 1 and not legacy.exists()
    svc = _second_engine(env, monkeypatch)
    try:
        svc.start()
        rt = _wait(lambda: (r := svc.runtime("alice")) is not None and r.active and r.db is not None and r)
        assert Path(rt.db._path).parent == current
        assert rt.db.epoch == epoch
        _wait(lambda: not rt.scanning and rt.db.count_by_status("local").get("indexed") == indexed)
    finally:
        svc.stop(budget_s=3.0)
    assert len(env.extracted) == extracted_before, "moved whole: nothing was extracted again"


def test_deleting_the_index_takes_the_pre_rename_copy_with_it(env):
    """A conflict kept an old copy next to the index in use; Settings →
    delete the local index removes both and closes the conflict, so the next
    boot moves nothing back."""
    (env.alice / "a.txt").write_text("\n\n".join(_para(i) for i in range(3)), encoding="utf-8")
    _start(env, "alice")
    _idle(env, "alice")
    current, legacy = _dirs()
    shutil.copytree(current, legacy)
    (legacy / "index.db.bak").write_bytes(b"an older copy")
    sysdir = svc_module.uds.system_dir()
    relocate.Journal.load(sysdir).record(
        f"index:{UID}", state=relocate.STATE_CONFLICT, kind="index", uid=UID, profile="alice",
        error="Profile id uid-alice has a Documentation search index in both places",
    )
    assert relocate.pending_errors(sysdir)

    # What Settings does: turn the folder off, then delete its index.
    env.storage.upsert_source("alice", "local", enabled=False)
    assert uds_state.request_purge("alice", "local") is True
    _wait(lambda: not current.exists() and not legacy.exists())

    assert relocate.pending_errors(sysdir) == []
    report = relocate.relocate(sysdir, [("alice", UID), ("bob", "uid-bob")])
    assert report.moved_indexes == 0 and not report.errors
    assert not current.exists() and not legacy.exists()
