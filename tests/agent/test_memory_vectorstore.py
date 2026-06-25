"""Unit tests for long-term memory in the vector store (embedding-on path).

Fakes the embedding provider and vector store so no model/server is needed;
patches the embedding gates (server flag + READY state) on.
"""

from __future__ import annotations

import app.agent.memory_vectorstore as mv


class _FakeEmbedding:
    def embed_documents(self, texts):
        return [[float(len(t)), 1.0, 2.0] for t in texts]  # 3-dim vectors

    def embed_query(self, text):
        return [float(len(text)), 1.0, 2.0]


class _FakeVectorStore:
    def __init__(self, existing=False):
        self._exists = existing
        self.points: list[dict] = []
        self.created: list[tuple] = []
        self.query_results: list[dict] = []  # returned for retrieve (limit > 1)
        self.dedup_hits: list[dict] = []      # returned for the dedup probe (limit == 1)

    def collection_exists(self, name):
        return self._exists

    def create_named_collection(self, name, size):
        self._exists = True
        self.created.append((name, size))
        return name

    def add_points(self, collection_name, points):
        self.points.extend(points)

    def query_by_vector(self, collection_name, vector, limit=10, filter=None):
        return self.dedup_hits if limit == 1 else self.query_results

    def list_all_points(self, collection_name, with_vectors=False, filter=None):
        return self.points


class _FakeAgent:
    def __init__(self, vs):
        self.embedding = _FakeEmbedding()
        self.vector_store = vs


def _enable(monkeypatch):
    monkeypatch.setattr(mv.BaseConfig, "is_embedding_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(mv.embedding_state, "is_ready", lambda: True)


def test_store_creates_collection_and_adds(monkeypatch):
    _enable(monkeypatch)
    vs = _FakeVectorStore(existing=False)
    agent = _FakeAgent(vs)
    n = mv.store_long_term(
        agent=agent, profile="admin", conversation_id="c1",
        facts=["User is Lee", "Likes tea"],
    )
    assert n == 2
    assert vs.created and vs.created[0][1] == 3  # collection sized to the embedding dim
    assert [p["payload"]["text"] for p in vs.points] == ["User is Lee", "Likes tea"]
    assert all(p["payload"]["profile"] == "admin" for p in vs.points)


def test_store_dedups_near_duplicate(monkeypatch):
    _enable(monkeypatch)
    vs = _FakeVectorStore(existing=True)
    vs.dedup_hits = [{"text": "User is Lee", "score": 0.99}]  # over threshold
    agent = _FakeAgent(vs)
    n = mv.store_long_term(
        agent=agent, profile="admin", conversation_id="c1", facts=["User is Lee"],
    )
    assert n == 0
    assert vs.points == []


def test_retrieve_sorts_oldest_first(monkeypatch):
    _enable(monkeypatch)
    vs = _FakeVectorStore(existing=True)
    vs.query_results = [
        {"text": "newer fact", "created_at": 200.0, "score": 0.9},
        {"text": "older fact", "created_at": 100.0, "score": 0.8},
    ]
    agent = _FakeAgent(vs)
    out = mv.retrieve_long_term(agent=agent, profile="admin", query_text="hi", limit=10)
    assert [e["content"] for e in out] == ["older fact", "newer fact"]


def test_noop_when_embedding_disabled(monkeypatch):
    monkeypatch.setattr(mv.BaseConfig, "is_embedding_enabled", classmethod(lambda cls: False))
    agent = _FakeAgent(_FakeVectorStore(existing=True))
    assert mv.store_long_term(
        agent=agent, profile="admin", conversation_id="c1", facts=["x"]
    ) == 0
    assert mv.retrieve_long_term(agent=agent, profile="admin", query_text="x") == []


def test_noop_when_query_empty(monkeypatch):
    _enable(monkeypatch)
    agent = _FakeAgent(_FakeVectorStore(existing=True))
    assert mv.retrieve_long_term(agent=agent, profile="admin", query_text="  ") == []
