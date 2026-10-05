"""Google Places builds its place-type table with the model the server loaded.

It used to construct its own ``LocalEmbeddings()``, so every boot with Vector
Embedding loaded the model twice — ~7 s more on a small Windows VM, and a
second copy held in memory. With the table's cache miss
(``tests/vectorstores/test_cached_table_chroma.py``) that pushed a boot past
the desktop app's wait for its backend, so a restart never came back.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.config.settings import BaseConfig
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import ToolStorage
from app.tools.builtin import gg_places, register_builtin_tools
from app.tools.config_manager import ToolConfigManager
from app.tools.registry import ToolRegistry


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return [1.0, 0.0, 0.0]


@pytest.fixture
def no_second_model(monkeypatch):
    """Vector Embedding on, and loading another model copy fails the test."""
    monkeypatch.setattr(BaseConfig, "is_embedding_enabled", lambda: True)

    def _second_load(*args, **kwargs):
        raise AssertionError("the embedding model was loaded a second time")

    monkeypatch.setattr("app.lib.embedding.LocalEmbeddings", _second_load)


def test_the_type_table_is_built_with_the_model_it_is_given(no_second_model):
    model = _CountingEmbedder()

    vendor, table = gg_places._build_embedding_table(vector_store=None, embedding=model)

    assert vendor is model
    assert len(table) == len(gg_places.INCLUDED_TYPES_TABLE)
    assert model.calls == len(gg_places.INCLUDED_TYPES_TABLE)


def test_registration_hands_google_places_the_boot_model(tmp_path: Path, monkeypatch):
    seen: list[tuple[object, object]] = []

    def _prepare_tools(vector_store=None, embedding=None):
        seen.append((vector_store, embedding))
        return None

    monkeypatch.setattr(gg_places, "get_prepare_tools", _prepare_tools)
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('admin', 'admin', :n, :n)"),
            {"n": now},
        )
    registry = ToolRegistry(storage, ToolConfigManager(storage))
    store, model = object(), object()

    asyncio.run(register_builtin_tools(
        registry=registry,
        config_manager=registry.config,
        llm_factory=lambda *a, **k: None,
        setup_profile="admin",
        vector_store=store,
        embedding=model,
    ))

    assert seen == [(store, model)]
