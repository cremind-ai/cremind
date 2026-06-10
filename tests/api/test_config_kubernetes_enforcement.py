"""Kubernetes storage-enforcement seams.

In Kubernetes mode (INSTALL_MODE=kubernetes) pods scale horizontally, so any
pod-local storage breaks data consistency. The wizard's ``kubernetes``
mode-rule hides the offending options in the UI, but the first-setup endpoint
is unauthenticated and config can also be written via the Settings page / CLI /
bulk import. These tests pin the defense-in-depth backend seams that reject
pod-local storage regardless of how the request arrives:

  Seam A — app.api.config._kubernetes_sqlite_rejection (wizard DB choice)
  Seam B — app.databases.factory.create_database_provider (boot/consume backstop)
  Seam C — app.lib.embedding_lifecycle.persist_embedding_config (embedding write)
  Seam D — app.cli.commands.db.db_upgrade (manual CLI migration)
  Seam E — app.vectorstores.factory.create_vector_store_client (consume backstop)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── Seam A — wizard DB-provider choice ────────────────────────────────────


def test_seam_a_rejects_sqlite_in_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.setattr(config_api, "is_kubernetes_mode", lambda: True)
    err = config_api._kubernetes_sqlite_rejection("sqlite")
    assert err is not None
    assert "SQLite" in err


def test_seam_a_allows_postgres_in_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.setattr(config_api, "is_kubernetes_mode", lambda: True)
    assert config_api._kubernetes_sqlite_rejection("postgres") is None


def test_seam_a_noop_when_not_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.setattr(config_api, "is_kubernetes_mode", lambda: False)
    assert config_api._kubernetes_sqlite_rejection("sqlite") is None


# ── Seam B — database factory backstop ────────────────────────────────────


def test_seam_b_factory_rejects_sqlite_in_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.databases import factory as db_factory

    monkeypatch.setattr(db_factory, "resolve_bootstrap", lambda: {"db_provider": "sqlite"})
    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    with pytest.raises(ValueError, match="SQLite is not supported in Kubernetes"):
        db_factory.create_database_provider()


# ── Seam C — embedding persist ────────────────────────────────────────────


def _fake_storage() -> SimpleNamespace:
    return SimpleNamespace(set=lambda *a, **k: None)


def test_seam_c_rejects_native_chroma_in_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lib import embedding_lifecycle

    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {"provider": "chroma", "chroma": {"deployment_mode": "native"}},
    }
    with pytest.raises(ValueError, match="only external"):
        embedding_lifecycle.persist_embedding_config(body, _fake_storage())


def test_seam_c_allows_external_in_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lib import embedding_lifecycle

    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {
            "provider": "qdrant",
            "deployment_mode": "external",
            "qdrant": {"deployment_mode": "external", "host": "cremind-qdrant", "port": 6333},
        },
    }
    # Should not raise — external is the one allowed mode on Kubernetes.
    embedding_lifecycle.persist_embedding_config(body, _fake_storage())


def test_seam_c_disabled_embeddings_skip_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embeddings-off default must never trip the K8s guard."""
    from app.lib import embedding_lifecycle

    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    embedding_lifecycle.persist_embedding_config({"enabled": False}, _fake_storage())


# ── Seam D — `cremind db upgrade` CLI ─────────────────────────────────────


def test_seam_d_db_upgrade_refuses_sqlite_default_in_kubernetes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from app.cli.commands import db as db_cmd

    monkeypatch.setattr("app.config.bootstrap.bootstrap_exists", lambda: False)
    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    # write_bootstrap must NOT be called, and migrations must not run.
    monkeypatch.setattr(
        "app.config.bootstrap.write_bootstrap",
        lambda *_a, **_k: pytest.fail("write_bootstrap should not run in K8s mode"),
    )
    with pytest.raises(typer.Exit) as exc:
        db_cmd.db_upgrade("head")
    assert exc.value.exit_code == 1


# ── Seam E — vectorstore factory backstop ─────────────────────────────────


def test_seam_e_factory_rejects_persistent_chroma_in_kubernetes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config.settings import BaseConfig
    from app.vectorstores import factory as vs_factory

    monkeypatch.setattr(BaseConfig, "get_vectorstore_provider", classmethod(lambda cls: "chroma"))
    monkeypatch.setattr(BaseConfig, "get_chroma_mode", classmethod(lambda cls: "persistent"))
    monkeypatch.setattr("app.config.install_catalog.is_kubernetes_mode", lambda: True)
    with pytest.raises(ValueError, match="Persistent .*ChromaDB is not supported"):
        vs_factory.create_vector_store_client()


# ── Postgres password auto-wiring (Option B) ──────────────────────────────


def test_injected_postgres_password_fills_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.setenv("CREMIND_POSTGRES_PASSWORD", "from-secret")
    pg = {"host": "cremind-postgresql", "database": "cremind", "user": "cremind", "password": ""}
    config_api._apply_injected_postgres_password(pg)
    assert pg["password"] == "from-secret"


def test_injected_postgres_password_does_not_override_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.setenv("CREMIND_POSTGRES_PASSWORD", "from-secret")
    pg = {"password": "typed-by-user"}
    config_api._apply_injected_postgres_password(pg)
    assert pg["password"] == "typed-by-user"


def test_injected_postgres_password_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import config as config_api

    monkeypatch.delenv("CREMIND_POSTGRES_PASSWORD", raising=False)
    pg = {"password": ""}
    config_api._apply_injected_postgres_password(pg)
    assert pg["password"] == ""
