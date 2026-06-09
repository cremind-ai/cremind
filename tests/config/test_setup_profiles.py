"""Unit tests for setup-wizard environment profiles.

Focus on the ``kubernetes`` profile added for Helm deployments: it must
pre-fill PostgreSQL (never SQLite), keep embeddings off by default, and never
seed a pod-local (persistent/native) vector store.
"""

from __future__ import annotations

import pytest

from app.config import setup_profiles


def test_kubernetes_profile_exists() -> None:
    profile = setup_profiles.get_setup_profile("kubernetes")
    assert profile is not None
    assert profile["id"] == "kubernetes"


def test_kubernetes_profile_uses_postgres_not_sqlite() -> None:
    profile = setup_profiles.get_setup_profile("kubernetes")
    server = profile["server_config"]
    assert server["db_provider"] == "postgres"
    assert server["postgres"]["deployment_mode"] == "external"


def test_kubernetes_profile_embeddings_off_by_default() -> None:
    profile = setup_profiles.get_setup_profile("kubernetes")
    assert profile["embedding_config"]["enabled"] is False


def test_kubernetes_profile_vectorstores_are_external_only() -> None:
    """If a user enables embeddings, the pre-filled vector stores must be
    external (HTTP) — never a pod-local persistent/native file."""
    vs = setup_profiles.get_setup_profile("kubernetes")["embedding_config"]["vectorstore"]
    assert vs["deployment_mode"] == "external"
    assert vs["qdrant"]["deployment_mode"] == "external"
    assert vs["chroma"]["deployment_mode"] == "external"
    # Belt-and-suspenders: the legacy chroma.mode hint must not be persistent.
    assert vs["chroma"].get("mode") != "persistent"


def test_active_setup_profile_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUP_WIZARD_ENV", "kubernetes")
    assert setup_profiles.get_active_setup_profile_id() == "kubernetes"
