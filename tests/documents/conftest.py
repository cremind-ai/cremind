"""Isolate the process-wide embedding state for every document test.

``DocumentSyncService`` resolves its vector store and embedding model from the
``embedding_state`` singleton at call time, so a test that leaves the singleton
READY would silently flip a later test from degraded full-scan mode to vector
mode — an order-dependent failure that is miserable to track down. Reset it on
both sides of every test in this package.

``_publish_safe`` is patched out because the state mutators fan their snapshot
out to an SSE bus, which lazily imports ``app.events`` (and through it
``app.storage``). That is pointless in a unit test and slow. The mutators look
the function up as a module global, so patching the attribute is enough — if it
is ever refactored into an import-inside-method, this fixture stops protecting
anything and the import cost comes back.
"""

from __future__ import annotations

import pytest

from app.config import embedding_state as embedding_state_module


@pytest.fixture(autouse=True)
def isolated_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding_state_module, "_publish_safe", lambda snapshot: None)
    embedding_state_module.embedding_state.mark_disabled()
    yield
    embedding_state_module.embedding_state.mark_disabled()
