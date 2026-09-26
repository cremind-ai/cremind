"""Copying a profile's ``ud_*`` collection to its ``doc_*`` name.

Against the real adapters (in-memory Qdrant, a throwaway Chroma directory) and
a real index DB, because what matters is exactly what a store gives back: the
same point ids, vectors and payloads in the new collection, the index row
switched only after a verified copy, the old collection dropped as a separate,
resumable cleanup — and nothing at all when the store is down or storage is
short, so search keeps using the old collection.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.documents import vector_migrate as vm
from app.documents import vector_sync, vectors
from app.documents.index import IndexDB
from app.documents.vectors import PAYLOAD_INDEXES, collection_name, make_payload, parse_collection_name

UID = "a1a1a1a1-0000-4000-8000-000000000001"
DIM = 4
STORE_KEY = "test-store"
MODEL = "intfloat/multilingual-e5-base"

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _legacy_name(uid: str, epoch: str, model_key: str, dim: int, monkeypatch) -> str:
    """The name the pre-rename code gave the collection (``ud_`` prefix, with
    the 63-char budget computed for that prefix)."""
    with monkeypatch.context() as m:
        m.setattr(vectors, "COLLECTION_PREFIX", vectors.LEGACY_COLLECTION_PREFIX)
        return collection_name(uid, epoch, model_key, dim)


def _vec(i: int) -> list[float]:
    raw = [math.sin(i + k) + 1.5 for k in range(DIM)]
    n = math.sqrt(sum(x * x for x in raw))
    return [x / n for x in raw]


def _payload(i: int) -> dict:
    return make_payload(source="local" if i % 2 else "drive", ctype="body", file_id=1000 + i,
                        folder_id=7, kind="pdf", day=19000 + i)


@pytest.fixture(params=["qdrant", "chroma"])
def store(request, tmp_path):
    from app.vectorstores.base import VectorStore

    if request.param == "qdrant":
        qdrant_client = pytest.importorskip("qdrant_client")
        from app.vectorstores.qdrant import QdrantClient

        raw = qdrant_client.QdrantClient(location=":memory:")
        yield VectorStore(client=QdrantClient(size=0, client=raw))
        raw.close()
    else:
        chromadb = pytest.importorskip("chromadb")
        from app.vectorstores.chroma import ChromaClient

        raw = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        yield VectorStore(client=ChromaClient(size=0, client=raw))
        try:
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def emb():
    return SimpleNamespace(model_key=MODEL, dimension=DIM)


@pytest.fixture
def live(monkeypatch, store, emb):
    """The engine's view of the live store; toggle ``state['up']`` to cut it."""
    state = {"up": True}
    monkeypatch.setattr(vector_sync, "live_handles", lambda: (emb, store) if state["up"] else None)
    monkeypatch.setattr(vector_sync, "store_key", lambda: STORE_KEY)
    monkeypatch.setattr(vm, "CLEANUP_DELAY_S", 0.0)
    monkeypatch.setattr(vm, "_capacity_ok", lambda rt, store, src, dim: True)
    return state


class _RT:
    def __init__(self, db, uid=UID, profile="alice"):
        self.db = db
        self.uid = uid
        self.profile = profile
        self.paused_user = False
        self.level = "ok"
        self.service = SimpleNamespace(note_enospc=lambda rt: None)


def _setup(tmp_path, store, monkeypatch, *, n=600, uid=UID, model=MODEL):
    db = IndexDB.open(str(tmp_path / uid / "index.db"), profile_uid=uid)
    legacy = _legacy_name(uid, db.epoch, model, DIM, monkeypatch)
    store.ensure_collection(legacy, DIM, payload_indexes=PAYLOAD_INDEXES)
    ids = list(range(1, n + 1))
    store.upsert_vectors(legacy, ids, [_vec(i) for i in ids], [_payload(i) for i in ids])
    gen = db.next_gen()
    db.add_collection(gen, legacy, model_key=model, dim=DIM, store_key=STORE_KEY, state="active")
    return db, legacy, gen


def _run(rt, *, limit=500, patience=5):
    """Step like the embedder loop does: keep going while there is work.

    A step that did nothing while a copy is still recorded is retried a few
    times, as the engine's next pass would: local Chroma now and then fails a
    read of a freshly written collection ("Error creating hnsw segment
    reader: Nothing found on disk") and serves the same read a moment later.
    A failure that persists still ends the loop and fails the test."""
    import time

    holds = []
    idle = 0
    for _ in range(limit):
        r = vm.step(rt)
        holds.append(r.hold)
        if r.work:
            idle = 0
            continue
        if rt.db.get_meta(vm.META_STATE) is None or idle >= patience:
            return holds
        idle += 1
        time.sleep(0.2)
    raise AssertionError("migration did not settle")


def _points(store, name, ids):
    got, _ = vm.retrieve_points(store, name, ids)
    return got


def test_the_collection_is_copied_verified_switched_and_the_old_one_dropped(tmp_path, store, live, monkeypatch):
    db, legacy, gen = _setup(tmp_path, store, monkeypatch)
    rt = _RT(db)
    target = collection_name(UID, db.epoch, MODEL, DIM)
    assert vm.pending(db)

    holds = _run(rt)

    active = db.active_collection()
    assert active["name"] == target and int(active["gen"]) == gen
    assert target.startswith("doc_") and len(target) <= 63
    assert legacy not in store.list_collections(), "the old collection is retired"
    assert store.count(target) == 600
    ids = list(range(1, 601))
    copied = _points(store, target, ids)
    assert set(copied) == set(ids)
    for i in (1, 2, 299, 600):
        vec, payload = copied[i]
        assert all(abs(a - b) < 1e-6 for a, b in zip(vec, _vec(i)))
        assert payload == _payload(i)
    assert not vm.pending(db)
    assert db.get_meta(vm.META_STATE) is None
    assert True in holds, "new vectors wait while the copy runs"
    # Chunks embedded in this generation stay embedded: same gen, new name.
    assert int(db.active_collection()["gen"]) == gen


def test_a_long_model_key_still_yields_a_valid_name(tmp_path, store, live, monkeypatch, emb):
    """``doc_`` is one character longer than ``ud_``: the 63-character budget
    is recomputed for it, the model part shortened with its hash kept."""
    long_model = "org/" + "very-long-embedding-model-name-" * 8 + "v2"
    emb.model_key = long_model
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=40, model=long_model)
    rt = _RT(db)

    _run(rt)

    name = db.active_collection()["name"]
    assert name.startswith("doc_") and len(name) <= vectors.MAX_COLLECTION_NAME
    assert parse_collection_name(name)[0] == parse_collection_name(legacy)[0]
    assert store.count(name) == 40


def test_nothing_happens_while_the_store_is_down_and_search_keeps_the_old_one(tmp_path, store, live, monkeypatch):
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=50)
    rt = _RT(db)
    live["up"] = False

    assert vm.step(rt).work == 0
    assert db.active_collection()["name"] == legacy
    assert db.get_meta(vm.META_STATE) is None

    live["up"] = True
    _run(rt)
    assert db.active_collection()["name"].startswith("doc_")


def test_a_store_failure_mid_copy_keeps_the_progress_and_resumes(tmp_path, store, live, monkeypatch):
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=900)
    rt = _RT(db)
    for _ in range(3):
        assert vm.step(rt).hold
    progress = vm.status(db)["state"]
    assert progress["phase"] == "copy" and progress["copied"] > 0

    real_scroll = store.scroll_ids

    def unreachable(*a, **kw):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(store, "scroll_ids", unreachable)
    r = vm.step(rt)
    assert r.work == 0 and not r.hold, "a failing store never blocks embedding"
    assert vm.status(db)["state"] == progress, "state kept as it was"
    assert db.active_collection()["name"] == legacy

    monkeypatch.setattr(store, "scroll_ids", real_scroll)
    _run(rt)
    assert store.count(db.active_collection()["name"]) == 900


def test_a_restart_resumes_from_the_persisted_progress(tmp_path, store, live, monkeypatch):
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=700)
    for _ in range(2):
        vm.step(_RT(db))
    copied = vm.status(db)["state"]["copied"]
    assert 0 < copied < 700
    path = db.path
    db.close()

    reopened = IndexDB.open(path, profile_uid=UID)
    rt = _RT(reopened)
    assert vm.status(reopened)["state"]["copied"] == copied
    _run(rt)
    assert store.count(reopened.active_collection()["name"]) == 700
    reopened.close()


def test_an_interrupted_cleanup_resumes(tmp_path, store, live, monkeypatch):
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=30)
    rt = _RT(db)
    real_delete = store.delete_collection
    monkeypatch.setattr(store, "delete_collection", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")))
    _run(rt)
    assert db.active_collection()["name"].startswith("doc_"), "the switch happened"
    assert legacy in store.list_collections(), "the drop failed..."
    assert vm.status(db)["cleanup"], "...and is still recorded"

    monkeypatch.setattr(store, "delete_collection", real_delete)
    _run(rt)
    assert legacy not in store.list_collections()
    assert not vm.pending(db)


def test_writes_during_the_copy_are_reconciled_before_the_switch(tmp_path, store, live, monkeypatch):
    """A point added to (or removed from) the source while it is being copied:
    counts differ at verify, the missing/extra passes fix the destination."""
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=300)
    rt = _RT(db)
    vm.step(rt)  # starts
    vm.step(rt)  # first page copied
    store.upsert_vectors(legacy, [5000], [_vec(5000)], [_payload(5000)])
    store.delete_ids(legacy, [1])

    _run(rt)

    target = db.active_collection()["name"]
    assert target.startswith("doc_")
    ids = [i for i in range(2, 301)] + [5000]
    assert store.count(target) == len(ids)
    got = _points(store, target, ids + [1])
    assert 1 not in got and 5000 in got


def test_short_storage_defers_the_copy(tmp_path, store, live, monkeypatch):
    monkeypatch.undo()  # the real capacity check
    monkeypatch.setattr(vector_sync, "live_handles", lambda: (SimpleNamespace(model_key=MODEL, dimension=DIM), store))
    monkeypatch.setattr(vector_sync, "store_key", lambda: STORE_KEY)
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=20)
    rt = _RT(db)

    rt.level = "budget"
    assert vm.step(rt).work == 0
    assert db.active_collection()["name"] == legacy and db.get_meta(vm.META_STATE) is None

    from app.documents import governor as gov

    rt.level = "ok"
    monkeypatch.setattr(gov, "measure_capacity", lambda *a, **kw: gov.Capacity(
        fs_free=100 * gov.MB, fs_total=500 * gov.GB, vector_capacity=None, db_capacity=None, method="statvfs"))
    monkeypatch.setattr("app.documents.settings.read_admin_policy",
                        lambda: SimpleNamespace(vector_capacity_mb=0, db_capacity_mb=0))
    assert vm.step(rt).work == 0, "free disk below the low-water floor"
    assert db.active_collection()["name"] == legacy


def test_a_generation_retired_mid_copy_is_abandoned_safely(tmp_path, store, live, monkeypatch):
    db, legacy, gen = _setup(tmp_path, store, monkeypatch, n=400)
    rt = _RT(db)
    vm.step(rt)
    vm.step(rt)
    partial = vm.status(db)["state"]["dst"]
    assert partial in store.list_collections()
    # A model change: vector_sync makes a new doc_ generation and retires ours.
    new_name = collection_name(UID, db.epoch, "other/model", DIM)
    store.ensure_collection(new_name, DIM)
    db.add_collection(db.next_gen(), new_name, model_key="other/model", dim=DIM,
                      store_key=STORE_KEY, state="active")

    vm.step(rt)

    assert db.get_meta(vm.META_STATE) is None
    assert partial not in store.list_collections(), "our partial copy is dropped"
    assert new_name in store.list_collections(), "the live generation is untouched"


def test_a_copy_that_never_verifies_stops_and_keeps_the_source(tmp_path, store, live, monkeypatch):
    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=20)
    rt = _RT(db)
    monkeypatch.setattr(vm, "_sample_matches", lambda *a, **kw: False)

    _run(rt)

    assert db.active_collection()["name"] == legacy
    assert db.get_meta(vm.META_FAILED)
    assert [n for n in store.list_collections() if n.startswith("doc_")] == []
    assert vm.step(rt).work == 0, "and it waits before trying again"


def test_two_profiles_migrate_independently(tmp_path, store, live, monkeypatch):
    other = "b2b2b2b2-0000-4000-8000-000000000002"
    db_a, legacy_a, _ = _setup(tmp_path, store, monkeypatch, n=50)
    db_b, legacy_b, _ = _setup(tmp_path, store, monkeypatch, n=70, uid=other)
    rt_a, rt_b = _RT(db_a), _RT(db_b, uid=other, profile="bob")

    _run(rt_a)

    assert db_b.active_collection()["name"] == legacy_b, "bob's index untouched"
    assert legacy_b in store.list_collections()
    _run(rt_b)
    a, b = db_a.active_collection()["name"], db_b.active_collection()["name"]
    assert store.count(a) == 50 and store.count(b) == 70
    assert parse_collection_name(a)[0] != parse_collection_name(b)[0]


def test_the_orphan_gc_keeps_both_sides_while_a_profile_is_live(tmp_path, store, live, monkeypatch):
    """The boot GC judges collections by uid tag alone, which ``ud_`` and
    ``doc_`` names share — so neither side of a migration is collected."""
    from app.documents import service as service_mod

    db, legacy, _ = _setup(tmp_path, store, monkeypatch, n=10)
    vm.step(_RT(db))
    target = vm.status(db)["state"]["dst"]
    assert parse_collection_name(legacy)[0] == parse_collection_name(target)[0]
    orphan = _legacy_name("dead-profile-uid", db.epoch, MODEL, DIM, monkeypatch)
    store.ensure_collection(orphan, DIM)
    monkeypatch.setattr(service_mod, "documents_root", lambda: str(tmp_path / "no-index-root"))
    monkeypatch.setattr(
        "app.storage.documents_storage.get_documents_storage",
        lambda: SimpleNamespace(profile_uids=lambda: {UID: "alice"}),
    )

    service_mod.DocumentsService._gc_orphan_indexes(None)

    names = store.list_collections()
    assert legacy in names and target in names
    assert orphan not in names


def test_rename_collection_is_compare_and_set(tmp_path):
    db = IndexDB.open(str(tmp_path / "x" / "index.db"), profile_uid=UID)
    gen = db.next_gen()
    db.add_collection(gen, "ud_old", model_key="m", dim=4, store_key="s", state="active")
    assert not db.rename_collection(gen, old="something-else", new="doc_new", meta={"k": "v"})
    assert db.get_meta("k") is None, "nothing written when the row moved on"
    assert db.rename_collection(gen, old="ud_old", new="doc_new", meta={"k": "v", "gone": None})
    assert db.active_collection()["name"] == "doc_new" and db.get_meta("k") == "v"
    db.close()
