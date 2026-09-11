"""The embedding rebuild indexes documents while the state is still REBUILDING.

``DocumentSyncService`` resolves its vector store and embedding model from the
``embedding_state`` singleton on every call, and only while that state is
READY. ``apply_embedding_config`` drops the owned collections and calls
``_rebuild_caches`` BEFORE ``mark_ready()``, so without help the reconcile
resolved to no store and silently indexed nothing into the collection it had
just dropped: documentation search stayed degraded until the next restart.
``_rebuild_caches`` now runs the reconcile inside ``service.pinned(...)`` with
the handles it was given. These pin both halves: the rebuild indexes, and the
pin never outlives the rebuild (a leaked override would keep the service on a
store the state later disowns).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.documents as documents_pkg
from app.config import embedding_state as embedding_state_module
from app.config.embedding_state import EmbeddingStatus
from app.documents.sync import COLLECTION_NAME, SHARED_SCOPE, DocumentSyncService
from app.lib import embedding_lifecycle

_DOC = '---\ndescription: "How to configure widgets."\n---\n\n# Widgets\n\nBody text.\n'


class _FakeStore:
    """Minimal VectorStore stand-in; it is its own ``_client``."""

    def __init__(self) -> None:
        self.has_collection = False
        self.created: list[tuple[str, int]] = []
        self.added: list[dict] = []
        self.deleted: list[list[int]] = []
        self._client = self

    def collection_exists(self, collection_name: str) -> bool:
        return self.has_collection

    def list_collections(self):
        return [COLLECTION_NAME] if self.has_collection else []

    def create_named_collection(self, *, collection_name, size):
        self.has_collection = True
        self.created.append((collection_name, size))

    def add_points(self, *, collection_name, points):
        self.added.extend(points)

    def list_all_points(self, *, collection_name, with_vectors=False, filter=None):
        return []

    def get_texts(self, *, collection_name, ids):
        return []

    def delete_texts(self, *, collection_name, ids):
        self.deleted.append(list(ids))


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str):
        self.calls += 1
        return [0.1, 0.2, 0.3]


@pytest.fixture
def rebuilding_state(monkeypatch):
    """The real singleton, parked in REBUILDING exactly as the apply leaves it.

    ``_publish_safe`` is a module global the mutators look up at call time;
    patching it keeps the SSE bus (and its ``app.events`` import) out of a unit
    test. ``monkeypatch`` is set up first, so it is still patched when the
    teardown below resets the state.
    """
    monkeypatch.setattr(embedding_state_module, "_publish_safe", lambda snapshot: None)
    state = embedding_state_module.embedding_state
    state.mark_disabled()
    assert state.mark_initializing()
    state.transition_to_rebuilding(phase="preparing_rebuild")
    assert state.status is EmbeddingStatus.REBUILDING
    assert not state.is_ready()
    yield state
    state.mark_disabled()


@pytest.fixture
def gg_places_calls(monkeypatch):
    """Stub the Places rebuild ``_rebuild_caches`` runs first; record its store."""
    calls: list[object] = []
    monkeypatch.setattr(
        "app.tools.builtin.gg_places._build_embedding_table",
        lambda vector_store=None: calls.append(vector_store),
    )
    return calls


def _write(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(_DOC, encoding="utf-8")


def _install_service(monkeypatch, tmp_path: Path) -> DocumentSyncService:
    """A real service over ``tmp_path``, served by ``app.documents.get_service``
    (which ``_rebuild_caches`` imports inside the function, at call time)."""
    service = DocumentSyncService(working_dir=tmp_path)
    monkeypatch.setattr(documents_pkg, "get_service", lambda: service)
    return service


def test_rebuild_indexes_documents_while_the_state_is_rebuilding(
    monkeypatch, tmp_path, rebuilding_state, gg_places_calls,
):
    _write(tmp_path / "documents", "widgets.md")
    service = _install_service(monkeypatch, tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()
    # The precondition that used to bite: the service sees no store at all.
    assert service._store() is None

    embedding_lifecycle._rebuild_caches(
        agent=None, embedding=emb, vector_store=store, profiles=[],
    )

    assert gg_places_calls == [store]
    assert store.created == [(COLLECTION_NAME, 3)]
    assert len(store.added) == 1, "the doc must be indexed although the state is not READY"
    point = store.added[0]
    assert point["vector"] == [0.1, 0.2, 0.3]
    assert point["payload"]["name"] == "widgets"
    assert point["payload"]["scope"] == SHARED_SCOPE
    assert emb.calls == 1

    # The pin is released: the state is still REBUILDING (only the apply marks
    # it READY), so the service is back to seeing nothing.
    assert rebuilding_state.status is EmbeddingStatus.REBUILDING
    assert rebuilding_state.phase is None
    assert service._store() is None
    assert service._embedder() is None


def test_rebuild_reindexes_each_listed_profile_and_no_other(
    monkeypatch, tmp_path, rebuilding_state, gg_places_calls,
):
    _write(tmp_path / "documents", "widgets.md")
    _write(tmp_path / "alice" / "documents", "alice-notes.md")
    _write(tmp_path / "bob" / "documents", "bob-notes.md")
    service = _install_service(monkeypatch, tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()

    embedding_lifecycle._rebuild_caches(
        agent=None, embedding=emb, vector_store=store, profiles=["alice"],
    )

    indexed = {(p["payload"]["scope"], p["payload"]["name"]) for p in store.added}
    assert indexed == {(SHARED_SCOPE, "widgets"), ("alice", "alice-notes")}
    assert service._store() is None


def test_override_is_cleared_when_the_reconcile_raises(
    monkeypatch, tmp_path, rebuilding_state, gg_places_calls,
):
    _write(tmp_path / "documents", "widgets.md")
    service = _install_service(monkeypatch, tmp_path)
    store, emb = _FakeStore(), _FakeEmbedder()
    seen_inside: list[tuple[object, object]] = []

    def _boom(scope):
        # Proves the failure happens INSIDE the pinned block.
        seen_inside.append((service._store(), service._embedder()))
        raise RuntimeError("store went away mid-rebuild")

    monkeypatch.setattr(service, "full_reconcile", _boom)

    # Swallowed and logged: a failed doc reconcile must not fail the apply.
    embedding_lifecycle._rebuild_caches(
        agent=None, embedding=emb, vector_store=store, profiles=["alice"],
    )

    assert seen_inside == [(store, emb)]
    assert service._store() is None
    assert service._embedder() is None
    assert rebuilding_state.phase is None
    assert store.added == []
