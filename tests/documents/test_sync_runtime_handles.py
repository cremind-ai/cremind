"""DocumentSyncService takes its embedding handles from the live state.

The bug these pin: the service used to capture the vector store and embedding
model at construction. Toggling Vector Embedding off in Settings clears the
state singleton and rewires the agent, but nothing could reach this service, so
documentation search kept querying a store the admin had just disabled — and an
install that enabled embedding without restarting kept skipping indexing
forever. Handles are now resolved per call, with an explicit ``pinned()``
escape hatch for the rebuild path (which runs while the state is REBUILDING,
i.e. deliberately not ready).
"""

from __future__ import annotations

from pathlib import Path

from app.config.embedding_state import embedding_state
from app.documents.sync import COLLECTION_NAME, SHARED_SCOPE, DocumentSyncService

_DOC = '---\ndescription: "A sample doc about widgets."\n---\n\nBody text.\n'


class _FakeStore:
    """Minimal VectorStore stand-in; it is its own ``_client``."""

    def __init__(self, *, has_collection: bool = True, hits=None):
        self.has_collection = has_collection
        self.hits = hits if hits is not None else [{"name": "vector-hit", "score": 0.9}]
        self.queries = 0
        self.created: list[tuple[str, int]] = []
        self.added: list[dict] = []
        self.deleted: list[list[int]] = []
        self._client = self

    def collection_exists(self, collection_name: str) -> bool:
        return self.has_collection

    def list_collections(self):
        return [COLLECTION_NAME] if self.has_collection else []

    def query_by_vector(self, *, collection_name, vector, limit, filter=None):
        self.queries += 1
        return list(self.hits)

    def list_all_points(self, *, collection_name, with_vectors=False, filter=None):
        return []

    def get_texts(self, *, collection_name, ids):
        return []

    def create_named_collection(self, *, collection_name, size):
        self.has_collection = True
        self.created.append((collection_name, size))

    def add_points(self, *, collection_name, points):
        self.added.extend(points)

    def delete_texts(self, *, collection_name, ids):
        self.deleted.append(list(ids))


class _FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed_query(self, text: str):
        self.calls += 1
        return [0.1, 0.2, 0.3]


def _service_with_doc(tmp_path: Path) -> DocumentSyncService:
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "widgets.md").write_text(_DOC, encoding="utf-8")
    return DocumentSyncService(working_dir=tmp_path)


def test_search_uses_state_handles_when_ready(tmp_path):
    svc = _service_with_doc(tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()
    embedding_state.mark_ready(emb, store)

    hits = svc.search(query="widgets", profile="admin", limit=5)

    assert hits == [{"name": "vector-hit", "score": 0.9}]
    assert store.queries == 1
    assert svc.last_search_mode == "vector"


def test_disable_at_runtime_degrades_the_same_instance(tmp_path):
    svc = _service_with_doc(tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()
    embedding_state.mark_ready(emb, store)
    svc.search(query="widgets", profile="admin", limit=5)
    assert store.queries == 1

    # The admin flips Vector Embedding off; no restart.
    embedding_state.mark_disabled()
    hits = svc.search(query="widgets", profile="admin", limit=5)

    assert store.queries == 1, "must not query a store the state has disowned"
    assert [h["name"] for h in hits] == ["widgets"], "falls back to the disk scan"
    assert svc.last_search_mode == "fallback-disabled"


def test_reconcile_is_a_noop_until_the_state_is_ready(tmp_path):
    svc = _service_with_doc(tmp_path)
    store, emb = _FakeStore(has_collection=False), _FakeEmbedder()

    svc.full_reconcile(SHARED_SCOPE)
    assert store.added == [], "nothing to index into while disabled"

    embedding_state.mark_ready(emb, store)
    svc.full_reconcile(SHARED_SCOPE)

    assert store.created == [(COLLECTION_NAME, 3)]
    assert len(store.added) == 1


def test_collection_is_created_again_in_a_swapped_store(tmp_path):
    """A cached "collection ready" flag would skip creation in the new store."""
    svc = _service_with_doc(tmp_path)
    emb = _FakeEmbedder()
    first = _FakeStore(has_collection=False)
    embedding_state.mark_ready(emb, first)
    svc.full_reconcile(SHARED_SCOPE)
    assert first.created == [(COLLECTION_NAME, 3)]

    second = _FakeStore(has_collection=False)
    embedding_state.mark_ready(emb, second)
    svc.full_reconcile(SHARED_SCOPE)

    assert second.created == [(COLLECTION_NAME, 3)]
    assert len(second.added) == 1


def test_constructor_override_wins_over_the_state(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "widgets.md").write_text(_DOC, encoding="utf-8")
    store, emb = _FakeStore(), _FakeEmbedder()
    svc = DocumentSyncService(working_dir=tmp_path, vector_store=store, embedding=emb)

    # State says disabled, but an explicitly injected store still wins.
    hits = svc.search(query="widgets", profile="admin", limit=5)

    assert store.queries == 1
    assert hits == [{"name": "vector-hit", "score": 0.9}]


def test_pinned_supplies_handles_and_restores_them(tmp_path):
    svc = _service_with_doc(tmp_path)
    emb = _FakeEmbedder()
    rebuild_store = _FakeStore(has_collection=False)

    # Mirrors the rebuild path: the state is NOT ready while this runs.
    with svc.pinned(vector_store=rebuild_store, embedding=emb):
        svc.full_reconcile(SHARED_SCOPE)

    assert len(rebuild_store.added) == 1
    assert svc._store() is None, "override must not outlive the block"


def test_pinned_restores_even_when_the_block_raises(tmp_path):
    svc = _service_with_doc(tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()

    class Boom(Exception):
        pass

    try:
        with svc.pinned(vector_store=store, embedding=emb):
            raise Boom()
    except Boom:
        pass

    assert svc._store() is None
    assert svc._embedder() is None


def test_store_error_degrades_instead_of_reporting_no_results(tmp_path):
    """An unreachable store used to read to the agent as "nothing matched"."""
    svc = _service_with_doc(tmp_path)
    emb = _FakeEmbedder()

    class _BrokenStore(_FakeStore):
        def query_by_vector(self, *, collection_name, vector, limit, filter=None):
            raise RuntimeError("connection refused")

    embedding_state.mark_ready(emb, _BrokenStore())
    hits = svc.search(query="widgets", profile="admin", limit=5)

    assert [h["name"] for h in hits] == ["widgets"]
    assert svc.last_search_mode == "fallback-error"


def test_missing_collection_degrades(tmp_path):
    svc = _service_with_doc(tmp_path)
    embedding_state.mark_ready(_FakeEmbedder(), _FakeStore(has_collection=False))

    hits = svc.search(query="widgets", profile="admin", limit=5)

    assert [h["name"] for h in hits] == ["widgets"]
    assert svc.last_search_mode == "fallback-no-collection"


def test_a_store_error_never_recreates_the_collection(tmp_path):
    """Both adapters implement create_named_collection as drop-and-recreate, and
    both report ANY error from collection_exists as "missing". Creating on that
    signal would wipe every indexed document during a network blip, so an error
    must skip the write instead."""
    svc = _service_with_doc(tmp_path)

    class _FlakyStore(_FakeStore):
        def collection_exists(self, collection_name):
            return False  # what the real adapters say when the call fails

        def list_collections(self):
            raise ConnectionError("store unreachable")

    store = _FlakyStore(has_collection=True)
    embedding_state.mark_ready(_FakeEmbedder(), store)

    svc.full_reconcile(SHARED_SCOPE)
    svc.apply_event(SHARED_SCOPE, tmp_path / "documents" / "widgets.md")

    assert store.created == [], "a failed existence check must not recreate"
    assert store.added == [], "and the write is skipped rather than attempted blind"
