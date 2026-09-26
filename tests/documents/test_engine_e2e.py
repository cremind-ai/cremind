"""The sync engine end to end: real extractor subprocesses, the real folder
watcher, a real (in-memory) Qdrant, and a deterministic fake embedder.

What must hold, in the user's words:

- adding a file makes it searchable, with vectors for every chunk;
- editing a few lines re-embeds only the changed chunks, not the document;
- renaming or moving a file keeps its chunks and vectors;
- deleting a file removes it (after a short grace period for slow moves);
- executables and secret-looking files are indexed by name only — the
  content of a secret file is never read;
- two profiles never see each other's files;
- switching the embedding model re-embeds from stored text with no
  re-extraction;
- many files vanishing at once is held for confirmation, not applied;
- stopping the engine is fast.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")
qdrant_client = pytest.importorskip("qdrant_client")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.storage.documents_storage as uds_storage_module  # noqa: E402
from app.config.embedding_state import embedding_state  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.documents_storage import DocumentsStorage  # noqa: E402
from app.documents import runtime as rt_module  # noqa: E402
from app.documents import service as svc_module  # noqa: E402
from app.documents import settings as uds  # noqa: E402
from app.vectorstores.base import VectorStore  # noqa: E402
from app.vectorstores.qdrant import QdrantClient  # noqa: E402
from app.config import working_dirs as working_dirs_module  # noqa: E402
from tests.documents._workspaces import install as install_working_dirs, move  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

_TABLES = ("profiles", "document_sources", "document_captions", "document_vision_usage")


class FakeEmbedder:
    """Deterministic unit vectors from a text hash; counts what it embeds."""

    def __init__(self, key: str = "fake_a", dim: int = 8):
        self._key = key
        self._dim = dim
        self.passages = 0

    @property
    def model_key(self) -> str:
        return self._key

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def provider_key(self) -> str:
        return "fake"

    def _vec(self, t: str) -> list[float]:
        h = hashlib.blake2b((self._key + t).encode(), digest_size=self._dim * 2).digest()
        v = [(h[2 * i] - 128) / 128.0 + 0.001 for i in range(self._dim)]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_passages(self, texts, *, batch_size=None):
        self.passages += len(texts)
        return [self._vec(t) for t in texts]

    def embed_search_query(self, text):
        return self._vec(text)

    embed_query = embed_search_query

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


def _wait(pred, timeout: float = 45.0, step: float = 0.2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def _para(i: int, words: int = 60) -> str:
    return " ".join(f"word{i}_{j}" for j in range(words)) + "."


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "system"
    wd = tmp_path / "work"
    for d in (sysdir, wd / "alice", wd / "bob"):
        d.mkdir(parents=True, exist_ok=True)
    # Each profile's working directory is the folder its index covers.
    working_dirs = install_working_dirs(monkeypatch, sysdir, {"alice": wd / "alice", "bob": wd / "bob"})

    provider = SqliteDatabaseProvider(str(tmp_path / "main.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for uid, name in (("uid-alice", "alice"), ("uid-bob", "bob")):
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,0,0)"
            ), {"i": uid, "n": name})
    storage = DocumentsStorage(provider)
    monkeypatch.setattr(uds_storage_module, "_instance", storage)

    rows = {"documentation_search.allowed": "true"}
    monkeypatch.setattr(uds, "get_dynamic", lambda table, key, *a, **k: rows.get(key))
    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", classmethod(lambda cls: True))

    # Fast housekeeping so tombstones and storage checks run inside a test.
    monkeypatch.setattr(svc_module, "HOUSEKEEPING_INTERVAL_S", 0.5)
    monkeypatch.setattr(rt_module, "TOMBSTONE_GRACE_S", 0.0)

    embedder = FakeEmbedder()
    raw = qdrant_client.QdrantClient(location=":memory:")
    store = VectorStore(client=QdrantClient(size=0, client=raw))
    embedding_state.mark_ready(embedder, store)

    def enable(profile: str, root: Path) -> None:
        storage.upsert_source(profile, "local", enabled=True,
                              root_path=os.path.realpath(root), first_sync_confirmed_at=1.0)

    svc = svc_module.DocumentsService()
    monkeypatch.setattr(svc_module, "_service", svc)
    extracted: list[str] = []
    real_extract = svc.extract

    def spy_extract(req, *, size):
        extracted.append(req.name)
        return real_extract(req, size=size)

    svc.extract = spy_extract
    ns = SimpleNamespace(
        svc=svc, storage=storage, wd=wd, alice=wd / "alice", bob=wd / "bob", working_dirs=working_dirs,
        embedder=embedder, store=store, raw=raw, extracted=extracted, enable=enable,
    )
    try:
        yield ns
    finally:
        svc.stop(budget_s=3.0)
        embedding_state.mark_disabled()
        raw.close()


def _start(env, *profiles):
    for p in profiles:
        env.enable(p, getattr(env, p))
    env.svc.start()
    for p in profiles:
        _wait(lambda: env.svc.runtime(p) is not None and env.svc.runtime(p).active)


def _idle(env, profile: str, timeout: float = 60.0):
    """Wait until the profile has no dirty files, nothing in flight, and every
    chunk has a vector."""
    def done():
        rt = env.svc.runtime(profile)
        if rt is None or rt.db is None or rt.scanning or rt.in_flight:
            return False
        counts = rt.db.count_by_status("local")
        if counts.get("dirty") or counts.get("tombstone"):
            return False
        st = rt.db.stats()
        return st["chunks"] > 0 and st["chunks_without_vectors"] == 0 and rt
    return _wait(done, timeout)


def _files(rt) -> dict[str, dict]:
    return {r["rel_path"]: r for r in rt.db.list_files(source="local", limit=1000)}


def _chunk_texts(rt) -> str:
    ids = []
    for f in rt.db.list_files(source="local", limit=1000):
        ids += [c.id for c in rt.db.get_chunks(int(f["id"]))]
    return "\n".join(r["text"] for r in rt.db.chunk_rows(ids))


def _collection_count(env, rt) -> int:
    coll = rt.db.active_collection()
    return env.store.count(coll["name"])


def test_indexes_text_formats_and_keeps_secrets_unread(env):
    (env.alice / "notes.md").write_text("# AI challenges\n\n" + "\n\n".join(_para(i) for i in range(6)),
                                        encoding="utf-8")
    (env.alice / "data.csv").write_text("region,revenue\nnorth,10\nsouth,20\n", encoding="utf-8")
    (env.alice / "tool.exe").write_bytes(b"MZ" + b"\x00" * 200)
    (env.alice / ".env").write_text("API_KEY=super-secret-value", encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")

    files = _files(rt)
    assert files["notes.md"]["status"] == "indexed"
    assert files["data.csv"]["status"] == "indexed"
    assert files["tool.exe"]["status"] == "metadata_only"
    body = _chunk_texts(rt)
    assert "word3_7" in body and "north" in body
    # Secret files: listed by name, content never read, never extracted.
    assert "super-secret-value" not in body
    assert ".env" not in env.extracted
    # Every chunk has exactly one vector.
    assert _collection_count(env, rt) == rt.db.stats()["chunks"]
    assert rt.db.fts_search('"word3_7"', limit=5)


def test_editing_a_few_lines_reembeds_only_the_changed_chunks(env):
    doc = env.alice / "long.md"
    paras = [_para(i) for i in range(80)]
    doc.write_text("# Report\n\n" + "\n\n".join(paras), encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")
    fid = int(_files(rt)["long.md"]["id"])
    before = {c.text_hash: c.id for c in rt.db.get_chunks(fid)}
    assert len(before) > 10
    embedded_before = env.embedder.passages

    paras[40] = paras[40].replace("word40_3", "EDITED")
    doc.write_text("# Report\n\n" + "\n\n".join(paras), encoding="utf-8")

    def reembedded():
        chunks = rt.db.get_chunks(fid)
        return any(c.text_hash not in before for c in chunks) and rt.db.stats()["chunks_without_vectors"] == 0
    _wait(reembedded)
    after = {c.text_hash: c.id for c in rt.db.get_chunks(fid)}
    new_hashes = set(after) - set(before)
    assert 1 <= len(new_hashes) <= 3, new_hashes
    # Unchanged chunks kept their rows (and so their vectors).
    kept = set(after) & set(before)
    assert all(after[h] == before[h] for h in kept)
    assert env.embedder.passages - embedded_before <= 3
    msgs = [e["message"] for e in rt.db.list_activity(limit=20)]
    assert any("chunks re-embedded" in m and "long.md" in m for m in msgs), msgs


def test_rename_keeps_chunks_and_delete_removes(env):
    (env.alice / "a.txt").write_text("\n\n".join(_para(i) for i in range(5)), encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _files(rt)["a.txt"]
    body_ids = {c.id for c in rt.db.get_chunks(int(row["id"])) if c.ordinal >= 0}

    os.replace(env.alice / "a.txt", env.alice / "b.txt")
    _wait(lambda: "b.txt" in _files(rt) and "a.txt" not in _files(rt))
    moved = _files(rt)["b.txt"]
    assert moved["cite_id"] == row["cite_id"]
    _wait(lambda: rt.db.count_by_status("local").get("dirty", 0) == 0 and not rt.in_flight)
    assert {c.id for c in rt.db.get_chunks(int(moved["id"])) if c.ordinal >= 0} == body_ids

    (env.alice / "b.txt").unlink()
    _wait(lambda: not _files(rt))
    _wait(lambda: _collection_count(env, rt) == rt.db.stats()["chunks"])


def test_profiles_are_isolated(env):
    (env.alice / "alice-secret-plan.txt").write_text("alice only " + _para(1), encoding="utf-8")
    (env.bob / "bob-notes.txt").write_text("bob only " + _para(2), encoding="utf-8")
    _start(env, "alice", "bob")
    a = _idle(env, "alice")
    b = _idle(env, "bob")
    assert set(_files(a)) == {"alice-secret-plan.txt"}
    assert set(_files(b)) == {"bob-notes.txt"}
    assert "alice only" not in _chunk_texts(b)
    assert a.db.path != b.db.path
    assert a.db.active_collection()["name"] != b.db.active_collection()["name"]


def test_model_change_reembeds_from_stored_text_without_reextracting(env):
    for i in range(3):
        (env.alice / f"f{i}.md").write_text("\n\n".join(_para(i * 10 + j) for j in range(6)), encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")
    old = rt.db.active_collection()
    extracted = len(env.extracted)

    new_embedder = FakeEmbedder(key="fake_b", dim=8)
    embedding_state.mark_ready(new_embedder, env.store)

    def reembedded():
        coll = rt.db.active_collection()
        return coll and coll["name"] != old["name"] and rt.db.stats()["chunks_without_vectors"] == 0
    _wait(reembedded)
    assert len(env.extracted) == extracted, "a model change must not re-extract files"
    assert new_embedder.passages >= rt.db.stats()["chunks"] - 1
    assert old["name"] not in env.store.list_collections()


def test_many_files_vanishing_at_once_are_held(env, monkeypatch):
    from app.documents.discovery.guard import RootGuard

    # The real floor is 200 missing files; 100 keeps the test quick while
    # exercising the same path.
    monkeypatch.setattr(RootGuard, "MASS_DELETE_MIN_FILES", 100)
    for i in range(210):
        (env.alice / f"n{i:03}.txt").write_text(f"note {i} " + _para(i, 8), encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice", timeout=120)
    assert len(_files(rt)) == 210

    rt._stop_watching()  # simulate what a scan (not the watcher) sees
    for i in range(150):
        (env.alice / f"n{i:03}.txt").unlink()
    rt.request_scan("test")
    _wait(lambda: rt.confirmation and rt.confirmation.get("kind") == "mass_delete")
    counts = rt.db.count_by_status("local")
    assert counts.get("missing") == 150
    assert counts.get("indexed") == 60

    env.svc.control("alice", "confirm_deletions")
    _wait(lambda: rt.db.count_by_status("local").get("missing", 0) == 0)
    assert len(_files(rt)) == 60


def test_stop_is_fast(env):
    (env.alice / "x.txt").write_text(_para(1), encoding="utf-8")
    _start(env, "alice")
    _idle(env, "alice")
    started = time.monotonic()
    env.svc.stop(budget_s=1.5)
    assert time.monotonic() - started < 3.0


# ── the folder is the working directory ────────────────────────────────────


def _held_for_root_change(rt) -> bool:
    return bool(rt and rt.hold and rt.hold.get("reason") == "pending_root_change"
                and rt.confirmation and rt.confirmation.get("kind") == "root_change")


def test_the_default_workspace_inside_the_system_dir_is_indexed(env):
    """Each profile's default working directory lives in the system folder,
    ``<SYS>/workspaces/<profile>``; that one folder there indexes, and each
    profile only its own."""
    move(env.working_dirs, "alice", None)
    move(env.working_dirs, "bob", None)
    alice_ws = Path(uds.validate_root("alice").path)
    bob_ws = Path(uds.validate_root("bob").path)
    assert uds.system_dir_exempt(str(alice_ws))
    (alice_ws / "plan.md").write_text("# Alice plan\n\n" + _para(1), encoding="utf-8")
    (bob_ws / "bob.md").write_text("# Bob notes\n\n" + _para(2), encoding="utf-8")
    env.enable("alice", alice_ws)
    env.enable("bob", bob_ws)
    env.svc.start()
    alice = _idle(env, "alice")
    bob = _idle(env, "bob")
    assert set(_files(alice)) == {"plan.md"}
    assert set(_files(bob)) == {"bob.md"}


def test_another_profiles_workspace_inside_the_folder_is_never_indexed(env, monkeypatch):
    """alice's folder holds the workspaces root (an upgraded admin whose legacy
    folder is the documents mount): bob's workspace in it is his alone."""
    env.working_dirs.rows["bob"] = None
    monkeypatch.setenv(working_dirs_module.WORKSPACES_ENV, str(env.alice / "workspaces"))
    working_dirs_module.invalidate()
    bob_ws = Path(uds.validate_root("bob").path)
    assert bob_ws == Path(os.path.realpath(env.alice / "workspaces" / "bob"))
    (bob_ws / "secret.md").write_text("# Bob private\n\n" + _para(7), encoding="utf-8")
    (env.alice / "mine.md").write_text("# Alice\n\n" + _para(3), encoding="utf-8")
    env.enable("alice", env.alice)
    env.enable("bob", bob_ws)
    env.svc.start()
    alice = _idle(env, "alice")
    bob = _idle(env, "bob")
    assert set(_files(alice)) == {"mine.md"}
    assert "word7_1" not in _chunk_texts(alice)
    assert set(_files(bob)) == {"secret.md"}


def test_a_folder_chosen_by_an_earlier_build_waits_for_confirmation(env):
    """A row saved with a folder of its own (root_mode "custom") now follows
    the working directory: nothing syncs, and nothing leaves the index, until
    the profile confirms the move."""
    old = env.wd / "old-choice"
    old.mkdir()
    (env.alice / "notes.md").write_text("# Notes\n\n" + _para(4), encoding="utf-8")
    env.storage.upsert_source("alice", "local", enabled=True, root_mode="custom",
                              root_path=os.path.realpath(old), first_sync_confirmed_at=1.0)
    env.svc.start()
    _wait(lambda: _held_for_root_change(env.svc.runtime("alice")))
    rt = env.svc.runtime("alice")
    assert rt.confirmation["from"] == os.path.realpath(old)
    assert rt.confirmation["to"] == os.path.realpath(env.alice)
    assert "working directory changed" in rt.confirmation["message"]
    assert not rt.active

    env.svc.control("alice", "confirm_root_change")
    rt = _idle(env, "alice")
    assert set(_files(rt)) == {"notes.md"}
    row = env.storage.get_source("alice", "local")
    assert (row["root_mode"], row["root_path"]) == ("inherit", os.path.realpath(env.alice))


def test_a_moved_working_directory_is_noticed_at_once_and_waits(env):
    """The admin moves alice's working directory: the change listener puts
    her folder on hold straight away (not at the next restart), with a count
    of what would leave the index; bob is untouched. Confirming indexes the
    new folder."""
    (env.alice / "a.md").write_text("# A\n\n" + _para(5), encoding="utf-8")
    (env.bob / "b.md").write_text("# B\n\n" + _para(6), encoding="utf-8")
    _start(env, "alice", "bob")
    _idle(env, "alice")
    _idle(env, "bob")

    moved = env.wd / "alice-new"
    moved.mkdir()
    (moved / "c.md").write_text("# C\n\n" + _para(8), encoding="utf-8")
    move(env.working_dirs, "alice", moved)
    _wait(lambda: _held_for_root_change(env.svc.runtime("alice")), timeout=15)
    rt = env.svc.runtime("alice")
    assert rt.confirmation["to"] == os.path.realpath(moved)
    assert (rt.confirmation["files"], rt.confirmation["leaving"]) == (1, 1)
    assert set(_files(rt)) == {"a.md"}, "nothing leaves the index before the confirmation"
    bob = env.svc.runtime("bob")
    assert bob.active and not bob.hold

    env.svc.control("alice", "confirm_root_change")
    _wait(lambda: set(_files(env.svc.runtime("alice"))) == {"c.md"})
    _idle(env, "alice")


def test_the_working_dir_listener_lives_with_the_engine(env):
    env.svc.start()
    assert env.svc._on_working_dir_changed in working_dirs_module._listeners
    env.svc.stop(budget_s=3.0)
    assert env.svc._on_working_dir_changed not in working_dirs_module._listeners


def test_a_profile_created_later_is_locked_out_of_a_folder_that_holds_it(env, monkeypatch):
    """alice's folder holds the workspaces root. carol is created after alice
    started indexing: the change listener re-checks alice's folder at once,
    and nothing carol puts in her workspace reaches alice's index."""
    monkeypatch.setenv(working_dirs_module.WORKSPACES_ENV, str(env.alice / "workspaces"))
    working_dirs_module.invalidate()
    (env.alice / "mine.md").write_text("# Alice\n\n" + _para(3), encoding="utf-8")
    _start(env, "alice")
    rt = _idle(env, "alice")
    assert not any("carol" in p for p in rt.locked_excludes)

    move(env.working_dirs, "carol", None)  # the profile is created
    carol = Path(uds.validate_root("carol").path)
    assert carol == Path(os.path.realpath(env.alice / "workspaces" / "carol"))
    _wait(lambda: any(os.path.normcase(str(carol)) == p for p in env.svc.runtime("alice").locked_excludes),
          timeout=15)
    (carol / "carol.md").write_text("# Carol private\n\n" + _para(9), encoding="utf-8")
    rt.request_scan("test")
    _wait(lambda: not rt.scanning and rt.scans_finished >= rt.scans_started)
    rt = _idle(env, "alice")
    assert set(_files(rt)) == {"mine.md"}
    assert "word9_1" not in _chunk_texts(rt)
