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
        self.deleted_filters: list[dict | None] = []

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
        # Honour the payload filter like a real store, so callers that select a
        # subset of points (e.g. one conversation's facts) are exercised
        # honestly rather than handed everything.
        if not filter:
            return list(self.points)
        return [
            p for p in self.points
            if all((p.get("payload") or {}).get(k) == v for k, v in filter.items())
        ]

    def delete_texts(self, collection_name, ids=None, filter=None, **kwargs):
        self.deleted_filters.append(filter)
        keep = []
        for p in self.points:
            payload = p.get("payload") or {}
            matches_filter = bool(filter) and all(
                payload.get(k) == v for k, v in filter.items()
            )
            matches_id = bool(ids) and p.get("id") in set(ids)
            if not (matches_filter or matches_id):
                keep.append(p)
        self.points = keep


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


# ── forgetting one conversation's facts ────────────────────────────────────
#
# This is what makes "delete this channel client" honest in embedding-on mode:
# long-term recall is filtered by profile alone, so a fact left behind here
# resurfaces in an unrelated conversation's prompt.


def _seeded(monkeypatch, *, profile="admin"):
    _enable(monkeypatch)
    vs = _FakeVectorStore(existing=True)
    agent = _FakeAgent(vs)
    mv.store_long_term(
        agent=agent, profile=profile, conversation_id="c-client",
        facts=["Client is Lee", "Client lives at 12 X St"],
    )
    mv.store_long_term(
        agent=agent, profile=profile, conversation_id="c-owner",
        facts=["Owner prefers metric units"],
    )
    return vs, agent


def test_forget_conversation_removes_only_that_conversations_facts(monkeypatch):
    vs, agent = _seeded(monkeypatch)

    removed = mv.forget_conversation(
        agent=agent, profile="admin", conversation_id="c-client",
    )

    assert removed == 2
    assert [p["payload"]["text"] for p in vs.points] == ["Owner prefers metric units"]
    # And the delete was scoped by profile as well as conversation.
    assert vs.deleted_filters == [
        {"profile": "admin", "source_conversation_id": "c-client"},
    ]


def test_forget_conversation_is_zero_when_nothing_matches(monkeypatch):
    vs, agent = _seeded(monkeypatch)
    assert mv.forget_conversation(
        agent=agent, profile="admin", conversation_id="c-nobody",
    ) == 0
    assert len(vs.points) == 3
    assert vs.deleted_filters == []  # no pointless delete call


def test_forget_conversation_ignores_other_profiles(monkeypatch):
    vs, agent = _seeded(monkeypatch)
    assert mv.forget_conversation(
        agent=agent, profile="other", conversation_id="c-client",
    ) == 0
    assert len(vs.points) == 3


def test_forget_conversation_needs_a_conversation_id(monkeypatch):
    vs, agent = _seeded(monkeypatch)
    assert mv.forget_conversation(agent=agent, profile="admin", conversation_id="") == 0
    assert len(vs.points) == 3


def test_forget_conversation_noop_when_embedding_disabled(monkeypatch):
    vs, agent = _seeded(monkeypatch)
    monkeypatch.setattr(mv.BaseConfig, "is_embedding_enabled", classmethod(lambda cls: False))
    assert mv.forget_conversation(
        agent=agent, profile="admin", conversation_id="c-client",
    ) == 0
    assert len(vs.points) == 3  # left for the DB path to handle


def test_forgotten_facts_stop_being_listed_back(monkeypatch):
    """The point of the delete: recall must no longer surface them."""
    vs, agent = _seeded(monkeypatch)
    mv.forget_conversation(agent=agent, profile="admin", conversation_id="c-client")
    listed = [e["content"] for e in mv.list_long_term(agent=agent, profile="admin")]
    assert listed == ["Owner prefers metric units"]
