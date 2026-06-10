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


# ── Kubernetes mode ──────────────────────────────────────────────────────


def test_kubernetes_is_a_rule_only_mode() -> None:
    """Kubernetes must define a mode-rule (so the chart's INSTALL_MODE=kubernetes
    constrains the wizard) but must NOT appear under [modes] — the host
    installers list every [modes] entry as a selectable option, and offering
    "Kubernetes" on a laptop would wrongly force Postgres-only on a desktop
    install."""
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert "kubernetes" in cat["mode_rules"]
    assert "kubernetes" not in cat["modes"], "kubernetes must not be host-selectable"
    # Host-selectable modes stay exactly docker + native.
    assert set(cat["modes"]) == {"docker", "native"}


def test_active_install_mode_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    assert install_catalog.get_active_install_mode() == "kubernetes"
    assert install_catalog.is_kubernetes_mode() is True


def test_is_kubernetes_mode_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    assert install_catalog.is_kubernetes_mode() is False


def test_is_kubernetes_mode_false_for_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALL_MODE", "docker")
    assert install_catalog.is_kubernetes_mode() is False


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


def test_mode_rule_kubernetes_external_only() -> None:
    """Kubernetes restricts every backing service to External (in-cluster
    service DNS). Crucially this drops Chroma's ``native`` mode — an in-process
    *persistent* local file that breaks horizontal scaling — without any
    per-service special-casing."""
    services = install_catalog.apply_mode_rule_to_services(_capabilities(), "kubernetes")
    assert services["postgres"]["supported_modes"] == ["external"]
    assert services["qdrant"]["supported_modes"] == ["external"]
    assert services["chroma"]["supported_modes"] == ["external"]  # 'native' (persistent) dropped
