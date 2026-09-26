"""Qdrant adapter: the documents vector primitives against in-memory Qdrant.

The shared contract (never drop, never create on an outage, filters, scores)
comes from ``_vector_contract``; this file adds what is Qdrant-specific: the
collection is created with on-disk vectors, int8 quantization, a small WAL and
integer payload indexes, and a create race ("already exists") reads as present.
"""

from __future__ import annotations

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from qdrant_client.http import models as qm  # noqa: E402

from app.lib.exception import VectorStoreException  # noqa: E402
from app.documents.vectors import PAYLOAD_INDEXES  # noqa: E402
from app.vectorstores.base import VectorStore  # noqa: E402
from app.vectorstores.qdrant import QdrantClient  # noqa: E402

from ._vector_contract import *  # noqa: E402,F401,F403 — the shared contract tests
from ._vector_contract import DIM, NAME, POINTS, _fill  # noqa: E402

# Local mode says so (once per call) for payload indexes and search params.
pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture
def adapter():
    raw = qdrant_client.QdrantClient(location=":memory:")
    try:
        yield QdrantClient(size=0, client=raw)
    finally:
        raw.close()


@pytest.fixture
def store(adapter):
    return VectorStore(client=adapter)


@pytest.fixture
def break_listing(adapter, monkeypatch):
    def _break():
        creates: list = []

        def _unreachable(*a, **kw):
            raise ConnectionError("qdrant unreachable")

        monkeypatch.setattr(adapter._client, "get_collections", _unreachable)
        monkeypatch.setattr(
            adapter._client, "create_collection", lambda *a, **kw: creates.append((a, kw)),
        )
        return creates

    return _break


def test_created_collection_uses_disk_vectors_int8_quantization_and_small_wal(adapter, monkeypatch):
    # Local mode does not keep these settings, so check what was asked for.
    seen: dict = {}
    real = adapter._client.create_collection

    def _spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(adapter._client, "create_collection", _spy)
    assert adapter.ensure_collection(NAME, DIM) == "created"

    vectors = seen["vectors_config"]
    assert vectors.size == DIM
    assert vectors.distance == qm.Distance.COSINE
    assert vectors.on_disk is True
    assert seen["on_disk_payload"] is True
    assert seen["hnsw_config"].m == 16
    assert seen["hnsw_config"].ef_construct == 100
    assert seen["wal_config"].wal_capacity_mb == 8
    scalar = seen["quantization_config"].scalar
    assert scalar.type == qm.ScalarType.INT8
    assert scalar.quantile == pytest.approx(0.99)
    assert scalar.always_ram is True


def test_query_rescores_quantized_candidates(adapter, monkeypatch):
    _fill(adapter)
    seen: dict = {}
    real = adapter._client.query_points

    def _spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(adapter._client, "query_points", _spy)
    adapter.query_vectors(NAME, POINTS[1][0], 3)
    params = seen["search_params"]
    assert params.hnsw_ef == 128
    assert params.quantization.rescore is True
    assert params.quantization.oversampling == pytest.approx(2.0)
    assert seen["with_payload"] is False


def test_payload_indexes_are_created_as_integer(adapter, monkeypatch):
    made: dict = {}
    real = adapter._client.create_payload_index

    def _record(*, collection_name, field_name, field_schema, **kw):
        made[field_name] = field_schema
        return real(collection_name=collection_name, field_name=field_name, field_schema=field_schema, **kw)

    monkeypatch.setattr(adapter._client, "create_payload_index", _record)
    adapter.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES)
    assert set(made) == {"f", "g", "s", "k", "t", "d"}
    assert all(v == qm.PayloadSchemaType.INTEGER for v in made.values())


def test_an_existing_index_is_fine_but_other_index_errors_raise(adapter, monkeypatch):
    adapter.ensure_collection(NAME, DIM)

    def _exists(**kw):
        raise RuntimeError("Index for field `f` already exists")

    monkeypatch.setattr(adapter._client, "create_payload_index", _exists)
    assert adapter.ensure_collection(NAME, DIM, payload_indexes={"f": "integer"}) == "present"

    def _broken(**kw):
        raise RuntimeError("No space left on device")

    monkeypatch.setattr(adapter._client, "create_payload_index", _broken)
    with pytest.raises(VectorStoreException, match="No space left"):
        adapter.ensure_collection(NAME, DIM, payload_indexes={"f": "integer"})


def test_losing_a_create_race_reads_as_present_without_dropping(adapter, monkeypatch):
    _fill(adapter)
    # Another writer created it between our listing and our create.
    monkeypatch.setattr(adapter, "list_collections", lambda: [])
    assert adapter.ensure_collection(NAME, DIM) == "present"
    assert adapter.count(NAME) == len(POINTS)


def test_store_errors_keep_their_message(adapter, monkeypatch):
    # The governor detects ENOSPC by message; no RetryError wrapping allowed.
    _fill(adapter)

    def _full(**kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(adapter._client, "upsert", _full)
    with pytest.raises(VectorStoreException, match="No space left on device"):
        adapter.upsert_vectors(NAME, [9], [POINTS[1][0]], [POINTS[1][1]])


def test_scroll_offset_is_qdrants_next_page_id(adapter):
    _fill(adapter)
    ids, nxt = adapter.scroll_ids(NAME, limit=2)
    assert ids == [1, 2]
    assert nxt == 3
    ids, nxt = adapter.scroll_ids(NAME, offset=nxt, limit=10)
    assert ids == [3, 4, 5]
    assert nxt is None


def test_large_upserts_are_split_into_batches(adapter, monkeypatch):
    adapter.ensure_collection(NAME, DIM)
    sizes: list[int] = []
    real = adapter._client.upsert

    def _spy(*, collection_name, points, **kw):
        sizes.append(len(points.ids))
        return real(collection_name=collection_name, points=points, **kw)

    monkeypatch.setattr(adapter._client, "upsert", _spy)
    n = 600
    vec, payload = POINTS[1]
    adapter.upsert_vectors(NAME, list(range(1, n + 1)), [vec] * n, [payload] * n)
    assert sizes == [256, 256, 88]
    assert adapter.count(NAME) == n
