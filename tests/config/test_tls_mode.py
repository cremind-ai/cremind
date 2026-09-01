"""The TLS facts the Setup Wizard is told, and their agreement with the boot path.

``_resolve_tls`` decides what this process serves; ``compute_tls_facts``
describes it to the wizard. They live in different modules because the request
handlers cannot import ``server``, which means the two can drift — so the last
test here walks the whole matrix and pins them together.
"""

from __future__ import annotations

import pytest

from app.config.settings import BaseConfig
from app.config.tls_mode import (
    MODE_AFTER_SETUP,
    MODE_AUTO,
    compute_tls_facts,
    current_tls_facts,
    effective_ssl_mode,
    env_supervised,
    https_origin_from_app_url,
)


@pytest.fixture(autouse=True)
def _no_ambient_supervisor(monkeypatch):
    """A dev box running under a boot service must not skew these answers."""
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)


# ── https_origin_from_app_url ────────────────────────────────────────────


@pytest.mark.parametrize(
    "app_url,expected",
    [
        ("http://localhost:1515", "https://localhost:1515"),
        ("https://localhost:1515", "https://localhost:1515"),
        ("http://cremind.lan:1515/", "https://cremind.lan:1515"),
        ("cremind.lan:1515", "https://cremind.lan:1515"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_https_origin_from_app_url(app_url, expected):
    assert https_origin_from_app_url(app_url) == expected


# ── mode normalisation ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["after-setup", "After-Setup", "  AFTER-SETUP  "])
def test_mode_is_normalised(raw, monkeypatch):
    """Operators type these into .env files by hand."""
    monkeypatch.setattr(BaseConfig, "SSL_MODE", raw, raising=False)
    assert effective_ssl_mode() == MODE_AFTER_SETUP


# ── compute_tls_facts ────────────────────────────────────────────────────


def _facts(**kw):
    base = dict(
        mode=MODE_AFTER_SETUP,
        has_pair=False,
        public_port=1515,
        serving_https=False,
        install_mode="kubernetes",
    )
    base.update(kw)
    return compute_tls_facts(**base)


def test_after_setup_before_the_switch_is_pending():
    facts = _facts()
    assert facts.pending_https is True
    assert facts.serving_https is False


def test_after_setup_once_serving_is_no_longer_pending():
    assert _facts(serving_https=True).pending_https is False


def test_pending_survives_the_window_between_bootstrap_and_restart():
    """The setup response is read in exactly this window.

    The wizard has just written bootstrap.toml, so the *next* boot will serve
    HTTPS — but this process still isn't, and the wizard still has a pivot to
    perform. Nothing here consults bootstrap.toml, which is what keeps that
    true.
    """
    assert _facts(serving_https=False).pending_https is True


def test_auto_is_never_pending():
    """auto either serves HTTPS already or was overridden — never 'about to'."""
    assert _facts(mode=MODE_AUTO).pending_https is False
    assert _facts(mode=MODE_AUTO, serving_https=True).pending_https is False


def test_plain_mode_is_never_pending():
    assert _facts(mode="").pending_https is False


def test_an_explicit_pair_is_not_pending():
    """A supplied certificate is served from boot; there is nothing to defer."""
    assert _facts(has_pair=True).pending_https is False


def test_no_public_bind_is_never_pending():
    """An external proxy owns the origin — this process will never serve TLS."""
    assert _facts(public_port=0).pending_https is False


def test_electron_is_never_pending(monkeypatch):
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "1")
    assert _facts().pending_https is False


@pytest.mark.parametrize(
    "install_mode,expected",
    [("docker", True), ("kubernetes", True), ("native", False), ("", False)],
)
def test_restart_supported_tracks_the_supervisor(install_mode, expected):
    """Only a supervised process comes back after the wizard restarts it."""
    assert _facts(install_mode=install_mode).restart_supported is expected


@pytest.mark.parametrize("install_mode", ["native", "custom", ""])
def test_a_boot_service_makes_any_install_restartable(install_mode):
    """`cremind boot enable` is the supervisor install_mode cannot describe.

    It says one thing about the process — something respawns me — which is
    true regardless of how Cremind was installed, so it is read on its own
    rather than as a modifier of the install mode.
    """
    facts = _facts(install_mode=install_mode, supervised=True)
    assert facts.restart_supported is True


def test_env_supervised_reads_the_unit_flag(monkeypatch):
    for raw in ("1", "true", "YES"):
        monkeypatch.setenv("CREMIND_SUPERVISED", raw)
        assert env_supervised() is True
    for raw in ("0", "false", "", "maybe"):
        monkeypatch.setenv("CREMIND_SUPERVISED", raw)
        assert env_supervised() is False
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)
    assert env_supervised() is False


def test_current_facts_pick_up_the_unit_flag(monkeypatch):
    """A hand-run `cremind serve` never sees the flag; the unit always does."""
    monkeypatch.setattr(BaseConfig, "SSL_MODE", MODE_AFTER_SETUP, raising=False)
    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)
    assert current_tls_facts(public_port=1515).restart_supported is False

    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    assert current_tls_facts(public_port=1515).restart_supported is True


# ── the anti-drift pin ───────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["", MODE_AUTO, MODE_AFTER_SETUP])
@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("public_port", [0, 1515])
def test_facts_agree_with_the_boot_path(
    mode, bootstrap, public_port, tmp_path, monkeypatch
):
    """``compute_tls_facts`` must describe what ``_resolve_tls`` actually does.

    They are separate implementations by necessity (the API layer cannot
    import ``server``), so this walks the matrix and asserts the one thing
    that must never disagree: whether TLS ends up being served.
    """
    from app import server

    monkeypatch.setattr(BaseConfig, "SSL_MODE", mode, raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://example.test", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_AUTO_HOSTS", [], raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setattr(server, "bootstrap_exists", lambda: bootstrap)
    monkeypatch.setattr(server.logger, "info", lambda *_a, **_kw: None)
    monkeypatch.setattr(server.logger, "warning", lambda *_a, **_kw: None)

    resolved = server._resolve_tls(None, None, public_port)
    serving = resolved is not None

    facts = compute_tls_facts(
        mode=mode,
        has_pair=False,
        public_port=public_port,
        serving_https=serving,
        install_mode="kubernetes",
    )
    assert facts.serving_https is serving
    # And a server that is serving TLS is never *also* pending it.
    assert not (facts.serving_https and facts.pending_https)
