"""Chroma adapter: the userdocs vector primitives against a real local Chroma.

The shared contract comes from ``_vector_contract``. Chroma-specific here: the
collection is created with ``hnsw:space=cosine`` explicitly (Chroma's default
is L2), and writes never go through ``get_or_create_collection`` — after a
wipe that would silently recreate the collection with L2.

Each test gets its own ``PersistentClient`` directory: ``EphemeralClient``
instances in one process share a single in-memory system, so collections
would leak between tests.
"""

from __future__ import annotations

import pytest

chromadb = pytest.importorskip("chromadb")

from app.lib.exception import VectorStoreException  # noqa: E402
from app.vectorstores.base import VectorStore  # noqa: E402
from app.vectorstores.chroma import ChromaClient, _collection_space  # noqa: E402

from ._vector_contract import *  # noqa: E402,F401,F403 — the shared contract tests
from ._vector_contract import DIM, NAME, POINTS, _fill  # noqa: E402


@pytest.fixture
def adapter(tmp_path):
    raw = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    try:
        yield ChromaClient(size=0, client=raw)
    finally:
        # Release the cached system (and its file handles) for this path.
        try:
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def store(adapter):
    return VectorStore(client=adapter)


@pytest.fixture
def break_listing(adapter, monkeypatch):
    def _break():
        creates: list = []

        def _unreachable(*a, **kw):
            raise ConnectionError("chroma unreachable")

        monkeypatch.setattr(adapter._client, "list_collections", _unreachable)
        monkeypatch.setattr(
            adapter._client, "get_or_create_collection", lambda *a, **kw: creates.append((a, kw)),
        )
        monkeypatch.setattr(
            adapter._client, "create_collection", lambda *a, **kw: creates.append((a, kw)),
        )
        return creates

    return _break


def test_created_collection_is_cosine(adapter):
    assert adapter.ensure_collection(NAME, DIM) == "created"
    assert _collection_space(adapter._client.get_collection(NAME)) == "cosine"


def test_a_present_collection_is_reused_not_recreated(adapter, monkeypatch):
    _fill(adapter)
    calls: list = []
    real = adapter._client.get_or_create_collection
    monkeypatch.setattr(
        adapter._client, "get_or_create_collection",
        lambda *a, **kw: calls.append(kw) or real(*a, **kw),
    )
    assert adapter.ensure_collection(NAME, DIM) == "present"
    assert calls == []
    assert adapter.count(NAME) == len(POINTS)


def test_a_non_cosine_collection_is_kept_as_is(adapter):
    # Someone made it without our metadata (L2). Never recreated here.
    col = adapter._client.get_or_create_collection(name=NAME)
    col.upsert(ids=["1"], embeddings=[POINTS[1][0]], metadatas=[POINTS[1][1]])
    assert adapter.ensure_collection(NAME, DIM) == "present"
    assert adapter.count(NAME) == 1


def test_writes_after_a_wipe_fail_instead_of_recreating_with_l2(adapter):
    _fill(adapter)
    adapter._client.delete_collection(name=NAME)

    with pytest.raises(VectorStoreException):
        adapter.upsert_vectors(NAME, [1], [POINTS[1][0]], [POINTS[1][1]])
    for op in (
        lambda: adapter.retrieve_vectors(NAME, [1]),
        lambda: adapter.delete_ids(NAME, [1]),
        lambda: adapter.scroll_ids(NAME),
        lambda: adapter.count(NAME),
        lambda: adapter.query_vectors(NAME, POINTS[1][0], 3),
    ):
        with pytest.raises(VectorStoreException):
            op()
    assert NAME not in adapter.list_collections()


def test_points_are_stored_without_documents(adapter):
    _fill(adapter)
    res = adapter._client.get_collection(NAME).get(ids=["1"], include=["documents", "metadatas"])
    assert res["documents"] == [None]
    assert res["metadatas"][0] == POINTS[1][1]


def test_scroll_offset_is_a_position(adapter):
    _fill(adapter)
    ids, nxt = adapter.scroll_ids(NAME, limit=2)
    assert len(ids) == 2 and nxt == 2
    ids2, nxt2 = adapter.scroll_ids(NAME, offset=nxt, limit=2)
    assert len(ids2) == 2 and nxt2 == 4
    ids3, nxt3 = adapter.scroll_ids(NAME, offset=nxt2, limit=2)
    assert len(ids3) == 1 and nxt3 is None
    assert sorted(ids + ids2 + ids3) == sorted(POINTS)


def test_store_errors_keep_their_message(adapter, monkeypatch):
    _fill(adapter)

    class _FullCollection:
        def upsert(self, **kw):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(adapter._client, "get_collection", lambda name: _FullCollection())
    with pytest.raises(VectorStoreException, match="No space left on device"):
        adapter.upsert_vectors(NAME, [9], [POINTS[1][0]], [POINTS[1][1]])
