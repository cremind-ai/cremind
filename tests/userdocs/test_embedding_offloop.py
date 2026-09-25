"""Chat-time embedding calls run off the event loop.

The shared embedder now serialises model calls behind a lock, and a User
Document Search batch may hold it for a few hundred milliseconds. The two chat
tools that embed a query synchronously — ``documentation_search`` (via the doc
service's ``search``) and ``search_memory`` (via ``retrieve_long_term``) — must
therefore wait in a worker thread, never on the event loop, where they would
stall every other request.
"""

from __future__ import annotations

import asyncio
import threading

import app.tools.builtin.documentation_search as ds
from app.agent import memory_vectorstore
from app.tools.builtin.search_memory import SearchMemoryTool


def test_documentation_search_runs_the_service_search_in_a_worker_thread(monkeypatch):
    seen: dict = {}

    class _Svc:
        last_search_mode = "vector"

        def search(self, *, query, profile, limit, scopes=None):
            seen["thread"] = threading.get_ident()
            seen["args"] = (query, profile, limit, scopes)
            return []

    monkeypatch.setattr(ds, "get_service", lambda: _Svc())

    async def run():
        seen["loop_thread"] = threading.get_ident()
        return await ds.run_doc_search(
            {"query": "how do I add a widget", "_profile": "alice", "top_k": 3},
            scopes=["shared"],
        )

    res = asyncio.run(run())
    assert res.structured_content["relevant"] is False  # no hits → no-result
    assert seen["args"] == ("how do I add a widget", "alice", 3, ["shared"])
    assert seen["thread"] != seen["loop_thread"]


def test_search_memory_retrieves_in_a_worker_thread(monkeypatch):
    seen: dict = {}

    def _retrieve(*, agent, profile, query_text, limit):
        seen["thread"] = threading.get_ident()
        seen["args"] = (profile, query_text, limit)
        return [{"content": "likes tea"}, {"content": ""}]

    monkeypatch.setattr(memory_vectorstore, "vector_long_term_available", lambda agent: True)
    monkeypatch.setattr(memory_vectorstore, "retrieve_long_term", _retrieve)

    async def run():
        seen["loop_thread"] = threading.get_ident()
        return await SearchMemoryTool().run({"query": "drinks", "_profile": "bob"})

    res = asyncio.run(run())
    assert res.structured_content == {"count": 1, "memories": ["likes tea"]}
    assert seen["args"] == ("bob", "drinks", 10)
    assert seen["thread"] != seen["loop_thread"]
