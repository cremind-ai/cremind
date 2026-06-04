"""Unit tests for the install/setup catalog loader."""

from __future__ import annotations

import pytest

from app.config import install_catalog


# ── load_install_catalog ──────────────────────────────────────────────────


def test_catalog_loads_expected_top_level_keys() -> None:
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert {"deployments", "modes", "mode_rules", "service_modes"} <= set(cat)


def test_catalog_contains_required_deployments() -> None:
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert set(cat["deployments"]) == {"local", "server", "custom"}


def test_catalog_does_not_define_container_deployment() -> None:
    """`container` was replaced by `custom` — make sure no reintroduction."""
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert "container" not in cat["deployments"]


def test_custom_deployment_has_advanced_fields() -> None:
    cat = install_catalog.load_install_catalog(force_reload=True)
    fields = cat["deployments"]["custom"]["advanced_fields"]
    keys = [f["key"] for f in fields]
    assert keys == ["listen_host", "public_url", "allowed_origins", "wizard_preset"]
    for field in fields:
        assert field["prompt"]
        assert field["hint"]


def test_fallback_when_file_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loader uses the hardcoded fallback when the TOML file is gone."""
    monkeypatch.setattr(install_catalog, "_CATALOG_FILE", install_catalog._CATALOG_FILE.parent / "does-not-exist.toml")
    install_catalog._cached = None
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert set(cat["deployments"]) == {"local", "server", "custom"}


# ── INSTALL_MODE resolution ───────────────────────────────────────────────


def test_active_install_mode_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    assert install_catalog.get_active_install_mode() is None


def test_active_install_mode_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALL_MODE", "   ")
    assert install_catalog.get_active_install_mode() is None


def test_active_install_mode_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALL_MODE", "docker")
    assert install_catalog.get_active_install_mode() == "docker"


def test_active_install_mode_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown values must not crash callers — return None."""
    monkeypatch.setenv("INSTALL_MODE", "nonsense")
    assert install_catalog.get_active_install_mode() is None


# ── mode rule application ────────────────────────────────────────────────


def _capabilities() -> dict[str, dict]:
    """Three-service fixture mirroring app.services.manifest shapes."""
    return {
        "postgres": {"display_name": "Postgres", "supported_modes": ["docker", "external"]},
        "chroma": {"display_name": "ChromaDB", "supported_modes": ["docker", "native", "external"]},
        "qdrant": {"display_name": "Qdrant", "supported_modes": ["docker", "external"]},
    }


def test_mode_rule_docker_hides_external() -> None:
    services = install_catalog.apply_mode_rule_to_services(_capabilities(), "docker")
    for svc in services.values():
        assert "external" not in svc["supported_modes"]


def test_mode_rule_native_hides_docker() -> None:
    services = install_catalog.apply_mode_rule_to_services(_capabilities(), "native")
    for svc in services.values():
        assert "docker" not in svc["supported_modes"]


def test_mode_rule_custom_keeps_all_modes() -> None:
    services = install_catalog.apply_mode_rule_to_services(_capabilities(), "custom")
    assert services["postgres"]["supported_modes"] == ["docker", "external"]
    assert services["chroma"]["supported_modes"] == ["docker", "native", "external"]


def test_mode_rule_none_is_passthrough() -> None:
    """No install mode → no filtering."""
    original = _capabilities()
    services = install_catalog.apply_mode_rule_to_services(original, None)
    assert services["postgres"]["supported_modes"] == ["docker", "external"]


def test_mode_rule_native_postgres_external_only() -> None:
    """Postgres has no native support; under Native install only External survives."""
    services = install_catalog.apply_mode_rule_to_services(_capabilities(), "native")
    assert services["postgres"]["supported_modes"] == ["external"]
