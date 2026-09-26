"""Everything that reaches a profile's Cremind manual pages goes by uuid.

The pages moved from ``<SYS>/<profile NAME>/documents`` to
``storage/cremind_documents/profiles/<profile uuid>``. These pin the other
places that address that directory — the agent's ``documents`` working
directory, profile deletion, the per-profile clean, and the watchers — with two
profiles, so a name-keyed leftover or a cross-profile path shows up here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cremind_documents import paths as doc_paths
from app.cremind_documents.sync import CremindDocumentSyncService, remove_profile_documents

UIDS = {"alice": "a1a1a1a1-0000-4000-8000-000000000001", "bob": "b2b2b2b2-0000-4000-8000-000000000002"}


@pytest.fixture
def sysdir(tmp_path, monkeypatch) -> Path:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(doc_paths, "resolve_profile_uid", lambda p: UIDS.get(p))
    return tmp_path


def _pages(base: Path, profile: str) -> Path:
    return base / "storage" / "cremind_documents" / "profiles" / UIDS[profile]


def test_the_documents_working_directory_is_the_profiles_own_uuid_directory(sysdir, monkeypatch):
    pytest.importorskip("a2a")
    from app.tools.builtin import change_working_directory as cwd_tool

    assert cwd_tool._resolve_target("documents", "alice") == _pages(sysdir, "alice")
    assert cwd_tool._resolve_target("documents", "bob") == _pages(sysdir, "bob")
    assert cwd_tool._resolve_target("documents", "ghost") is None

    published = []

    class _Bus:
        async def publish(self, *a):
            published.append(a)

    monkeypatch.setattr(cwd_tool, "get_event_stream_bus", lambda: _Bus())
    monkeypatch.setattr(cwd_tool, "get_user_working_directory", lambda *a, **k: str(sysdir))
    import app.events.runner as runner

    monkeypatch.setattr(runner, "get_conversation_storage", lambda: SimpleNamespace())
    res = asyncio.run(cwd_tool.ChangeWorkingDirectoryTool().run({
        "target": "documents", "_context_id": "conv-docs-cwd", "_profile": "bob",
    }))
    assert res.structured_content["current"] == str(_pages(sysdir, "bob"))
    assert _pages(sysdir, "bob").is_dir(), "created on first use"
    assert not (sysdir / "bob" / "documents").exists(), "never the old name-keyed place"

    denied = asyncio.run(cwd_tool.ChangeWorkingDirectoryTool().run({
        "target": "documents", "_context_id": "conv-docs-cwd-2", "_profile": "ghost",
    }))
    assert "Could not resolve" in denied.content[0]["text"]


class _Prunable(CremindDocumentSyncService):
    def __init__(self, base):
        super().__init__(working_dir=base, profile_uid_resolver=UIDS.get)
        self.pruned: list[str] = []

    def prune_scope(self, scope):
        self.pruned.append(scope)


def test_deleting_a_profile_removes_only_its_own_pages_and_points(sysdir):
    svc = _Prunable(sysdir)
    for who in ("alice", "bob"):
        _pages(sysdir, who).mkdir(parents=True)
        (_pages(sysdir, who) / "page.md").write_text(who, encoding="utf-8")
    assert svc.profile_dir("alice") == _pages(sysdir, "alice")  # cached now

    assert remove_profile_documents("alice", UIDS["alice"], service=svc) is True

    assert not _pages(sysdir, "alice").exists()
    assert (_pages(sysdir, "bob") / "page.md").read_text(encoding="utf-8") == "bob"
    assert svc.pruned == ["alice"]
    UIDS_BEFORE = dict(UIDS)
    try:
        UIDS["alice"] = "c3c3c3c3-new-alice"
        assert svc.profile_dir("alice").name == "c3c3c3c3-new-alice", "the cached uuid was dropped"
    finally:
        UIDS.clear()
        UIDS.update(UIDS_BEFORE)


def test_deleting_without_a_uuid_touches_no_directory(sysdir):
    svc = _Prunable(sysdir)
    _pages(sysdir, "bob").mkdir(parents=True)
    assert remove_profile_documents("bob", None, service=svc) is False
    assert _pages(sysdir, "bob").exists()
    assert remove_profile_documents("bob", "../..", service=svc) is False
    assert sysdir.exists()


def test_the_per_profile_clean_uses_the_uuid_directory(sysdir):
    pytest.importorskip("a2a")
    from app.reset import engine as reset_engine

    svc = _Prunable(sysdir)
    for who in ("alice", "bob"):
        _pages(sysdir, who).mkdir(parents=True)
        (_pages(sysdir, who) / "page.md").write_text(who, encoding="utf-8")

    detail = asyncio.run(reset_engine._clean_cremind_documents(
        "alice", SimpleNamespace(cremind_document_service=svc),
    ))

    assert detail == {"removed": True}
    assert not _pages(sysdir, "alice").exists()
    assert _pages(sysdir, "bob").exists()
    assert svc.pruned == ["alice"]

    ghost = asyncio.run(reset_engine._clean_cremind_documents(
        "ghost", SimpleNamespace(cremind_document_service=svc),
    ))
    assert ghost == {"removed": False}


def test_watchers_are_registered_per_scope_and_skip_unknown_profiles(sysdir):
    from app.cremind_documents import watcher as watcher_mod

    svc = CremindDocumentSyncService(working_dir=sysdir, profile_uid_resolver=UIDS.get)
    try:
        first = watcher_mod.start_scope_watcher(svc, "alice")
        assert first is not None and first.directory == _pages(sysdir, "alice")
        again = watcher_mod.start_scope_watcher(svc, "alice")
        assert again is not first, "re-arming replaces the old observer"
        assert watcher_mod.start_scope_watcher(svc, "ghost") is None
        assert not (sysdir / "ghost").exists()
        assert watcher_mod.stop_scope_watcher("alice") is True
        assert watcher_mod.stop_scope_watcher("alice") is False
    finally:
        watcher_mod.stop_scope_watcher("alice")
