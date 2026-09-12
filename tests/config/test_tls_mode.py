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
    public_app_url,
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


def test_electron_can_prepare_https(monkeypatch):
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "1")
    assert _facts().pending_https is True


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


# ── public_app_url ───────────────────────────────────────────────────────


@pytest.fixture
def stale(monkeypatch):
    """An install whose internal bind and public bind genuinely differ."""
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")


@pytest.mark.parametrize(
    "app_url,expected",
    [
        # The value Cremind's own v0.0.1 Docker installer wrote, and the
        # shapes it takes on a server install or an IPv6 host.
        ("http://localhost:1112", "http://localhost:1515"),
        ("https://localhost:1112", "https://localhost:1515"),
        ("http://cremind.lan:1112/", "http://cremind.lan:1515"),
        ("http://[::1]:1112", "http://[::1]:1515"),
        # Compose substitutes ${APP_URL} with the empty string when the key is
        # missing from the .env beside docker-compose.yml.
        ("", "http://localhost:1515"),
        ("   ", "http://localhost:1515"),
        ("http://[bad", "http://localhost:1515"),
        ("http://host:notaport", "http://localhost:1515"),
        ("not a url at all", "http://localhost:1515"),
    ],
)
def test_public_app_url_repairs_an_address_no_browser_can_open(
    app_url, expected, stale, monkeypatch,
):
    monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)

    assert public_app_url() == expected


@pytest.mark.parametrize(
    "app_url", ["http://localhost:1515", "https://cremind.lan", "http://testserver"],
)
def test_public_app_url_leaves_a_reachable_address_exactly_as_it_is(
    app_url, stale, monkeypatch,
):
    """Returned as the same object, so a caller can detect a repair with ``!=``
    and report only what it actually changed."""
    monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)

    assert public_app_url() is BaseConfig.APP_URL


def test_a_single_port_install_has_nothing_to_repair(monkeypatch):
    """Naming 1112 when 1112 *is* the public bind is just this install."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112", raising=False)
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    monkeypatch.setenv("CREMIND_UI_PORT", "1112")

    assert public_app_url() is BaseConfig.APP_URL


@pytest.mark.parametrize("app_url", ["http://localhost:1112", ""])
@pytest.mark.parametrize(
    "environment",
    [{"CREMIND_UI_PORT": "0"}, {"CREMIND_TLS_TERMINATION": "edge"}],
)
def test_a_deployment_owned_origin_is_not_ours_to_judge(app_url, environment, monkeypatch):
    """A reverse proxy owns the address, and the dev loop in CONTRIBUTING.md
    deliberately points APP_URL at the internal port with CREMIND_UI_PORT=0.

    Both signals have to be honoured, because ``https_target`` reads the raw
    APP_URL on exactly these deployments: repairing one and not the other would
    leave the transition's source and target origins naming different ports.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert public_app_url() is BaseConfig.APP_URL


@pytest.mark.parametrize("app_url", ["", "   ", "http://[bad", "not a url at all"])
def test_an_origin_that_reaches_this_server_beats_guessing_at_localhost(
    app_url, stale, monkeypatch,
):
    """With no hostname to keep, ``localhost`` is a guess and the address the
    administrator is actually using is a fact.

    It is used whole, port included: on a published Compose install that port
    may be a host-side mapping this process never sees.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)

    assert public_app_url(fallback="http://cremind.example.com:8443") == (
        "http://cremind.example.com:8443")
    # No port means the scheme's default port, not this process's public bind.
    assert public_app_url(fallback="http://cremind.example.com") == (
        "http://cremind.example.com")
    # A fallback that is itself unusable changes nothing.
    assert public_app_url(fallback="") == "http://localhost:1515"
    assert public_app_url(fallback="nonsense") == "http://localhost:1515"


def test_a_usable_app_url_ignores_the_fallback(stale, monkeypatch):
    """The fallback is for a missing address, never a second opinion about a
    configured one — the hostname in APP_URL is the operator's choice."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://cremind.lan:1112", raising=False)

    assert public_app_url(fallback="http://192.168.1.9:1515") == "http://cremind.lan:1515"


def test_the_internal_port_can_be_supplied_by_a_caller(stale, monkeypatch):
    """The boot path knows the ports it actually bound; it need not re-read
    them from configuration that may say something else."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:9999", raising=False)

    assert public_app_url(9999) == "http://localhost:1515"
    assert public_app_url(1112) is BaseConfig.APP_URL
