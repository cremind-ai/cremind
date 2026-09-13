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


def test_active_install_mode_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matched the way every other reader of the variable matches it.

    A hand-edited ``.env`` carrying ``INSTALL_MODE=Docker`` used to be "no mode
    at all" here — so the wizard offered the native service matrix — while
    ``app.config.tls_managed_env`` lower-cased and called the same install
    Docker.
    """
    monkeypatch.setenv("INSTALL_MODE", "  Docker ")
    assert install_catalog.get_active_install_mode() == "docker"

    monkeypatch.setenv("INSTALL_MODE", "Kubernetes")
    assert install_catalog.get_active_install_mode() == "kubernetes"
    assert install_catalog.is_kubernetes_mode() is True


# ── Kubernetes mode ──────────────────────────────────────────────────────


def test_kubernetes_mode_is_capability_gated() -> None:
    """Kubernetes is a selectable install mode, but only where it can run.

    It appears under [modes] so the install scripts and the TUI can offer it,
    and under [mode_rules] so the chart's INSTALL_MODE=kubernetes still
    constrains the wizard on the pod. What keeps it off a laptop is
    ``requires``: every front-end lists a mode only when it probed and found
    each named capability. ``native`` requires nothing, so some mode is always
    available.
    """
    cat = install_catalog.load_install_catalog(force_reload=True)
    assert "kubernetes" in cat["mode_rules"]
    assert "kubernetes" in cat["modes"]
    assert set(cat["modes"]) == {"docker", "native", "kubernetes"}
    assert cat["modes"]["kubernetes"]["requires"] == ["kubectl", "helm"]
    assert cat["modes"]["native"].get("requires", []) == []
    # Order is the recommendation: kubernetes must never outrank Docker.
    assert cat["modes"]["kubernetes"]["order"] > cat["modes"]["docker"]["order"]
    # No badge — "recommended" belongs to exactly one mode.
    assert not cat["modes"]["kubernetes"].get("badge")


def test_kubernetes_prompts_are_present() -> None:
    """The kubernetes-only questions the installers and the TUI render."""
    cat = install_catalog.load_install_catalog(force_reload=True)
    k8s = cat["kubernetes"]
    assert k8s["namespace_default"] == "cremind"
    assert k8s["context_prompt"] and k8s["context_hint"]
    fields = k8s["advanced_fields"]
    assert [f["key"] for f in fields] == [
        "release_name",
        "app_url",
        "legacy_postgres_image",
        "delete_postgres_data",
        "extra_set",
    ]
    for field in fields:
        assert field["prompt"]
        assert field["hint"]
    by_key = {f["key"]: f for f in fields}
    assert by_key["legacy_postgres_image"]["choices"] == ["yes", "no"]
    assert by_key["legacy_postgres_image"]["default"] == "yes"
    assert by_key["delete_postgres_data"]["default"] == "no"
    # The shells use a non-empty release name as "advanced was answered".
    assert by_key["release_name"]["default"] == "cremind"


def test_default_catalog_mirrors_the_toml_skeleton() -> None:
    """The hardcoded fallback must not drift from the shipped TOML.

    ``_DEFAULT_CATALOG`` is hand-maintained and only used when the TOML cannot
    be parsed — which is exactly when a silent disagreement would be hardest
    to notice. Compare the structure, not the prose.
    """
    import tomllib

    toml_cat = tomllib.loads(
        install_catalog._CATALOG_FILE.read_text(encoding="utf-8")
    )
    fallback = install_catalog._DEFAULT_CATALOG

    # Two tables the fallback has always left out: they carry only prompt text
    # for the Docker sub-questions, and the dataclasses that read them supply
    # their own defaults. Everything else must be mirrored, so a new top-level
    # table cannot be added to the TOML without deciding about the fallback.
    known_omissions = {"docker_desktop", "vnc_password"}
    assert set(toml_cat) - set(fallback) == known_omissions
    assert set(fallback) <= set(toml_cat)
    assert set(fallback["modes"]) == set(toml_cat["modes"])
    for mode_id, body in toml_cat["modes"].items():
        assert fallback["modes"][mode_id].get("requires", []) == body.get(
            "requires", []
        ), mode_id
        assert fallback["modes"][mode_id]["order"] == body["order"], mode_id
    assert fallback["mode_rules"] == toml_cat["mode_rules"]
    assert set(fallback["deployments"]) == set(toml_cat["deployments"])
    assert [f["key"] for f in fallback["kubernetes"]["advanced_fields"]] == [
        f["key"] for f in toml_cat["kubernetes"]["advanced_fields"]
    ]


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
