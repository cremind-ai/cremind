"""A cached embedding table loads back from Chroma instead of being re-embedded.

Chroma returns a ``get``'s embeddings as one numpy array, and
``list_all_points`` tested it with ``or []`` — which raises for an array. So
loading any cached table failed, and every boot with Vector Embedding
re-embedded the Google Places types (~10 s on a small Windows VM): part of
why a desktop restart outlasted the app's wait for its backend.

Each test gets its own ``PersistentClient`` directory, as in
``tests/documents/test_vectors_chroma.py``.
"""

from __future__ import annotations

import pytest

chromadb = pytest.importorskip("chromadb")

from app.vectorstores import get_or_build_embedding_table  # noqa: E402
from app.vectorstores.base import VectorStore  # noqa: E402
from app.vectorstores.chroma import ChromaClient  # noqa: E402

PLACES = {"cafe": "cafe", "bakery": "bakery", "train_station": "train station"}


class _CountingEmbedder:
    """Stands in for ``LocalEmbeddings``: one ``embed_query`` per text."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return [float(len(text)), 0.5, -0.25]


@pytest.fixture
def store(tmp_path):
    raw = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    try:
        yield VectorStore(client=ChromaClient(size=0, client=raw))
    finally:
        # Release the cached system (and its file handles) for this path.
        try:
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001
            pass


def test_points_come_back_with_their_vectors_as_plain_floats(store):
    store.create_named_collection(collection_name="places", size=2)
    store.add_points(collection_name="places", points=[
        {"id": 1, "vector": [0.25, 0.5], "payload": {"key": "cafe", "text": "cafe"}},
        {"id": 2, "vector": [0.75, 1.0], "payload": {"key": "bakery", "text": "bakery"}},
    ])

    points = store.list_all_points(collection_name="places", with_vectors=True)

    vectors = {p["payload"]["key"]: p["vector"] for p in points}
    assert vectors == {"cafe": [0.25, 0.5], "bakery": [0.75, 1.0]}
    assert all(type(x) is float for v in vectors.values() for x in v)


def test_the_next_boot_loads_the_table_instead_of_embedding_it_again(store):
    first_boot = _CountingEmbedder()
    built = get_or_build_embedding_table(
        vector_store=store, embedding=first_boot, data=PLACES, collection_name="gg_places_types",
    )
    assert first_boot.calls == len(PLACES)

    next_boot = _CountingEmbedder()
    loaded = get_or_build_embedding_table(
        vector_store=store, embedding=next_boot, data=PLACES, collection_name="gg_places_types",
    )

    assert next_boot.calls == 0
    rows = {row["id"]: list(row["embeddings"]) for _, row in built.dataframe.iterrows()}
    assert {row["id"]: list(row["embeddings"]) for _, row in loaded.dataframe.iterrows()} == rows
