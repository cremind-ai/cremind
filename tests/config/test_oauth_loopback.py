"""The loopback redirect every Google OAuth flow advertises (app/config/oauth_loopback.py).

Google's Desktop client accepts only ``http`` loopback redirects, so whatever
this returns goes into every Google consent URL Cremind builds — the five
skills (through the system variable), the Calendar connect and the Drive
Picker. A wrong answer is not a degraded flow: a public hostname or an ``https``
URI fails at Google with a 400 before the consent screen, and a portless origin
sends the browser to :80, which no deployment serves.
"""

from __future__ import annotations

import pytest

from app.config import oauth_loopback as ol
from app.config.settings import BaseConfig


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    """Every input the rule reads, from a known state.

    A developer shell exporting any of these (a pinned redirect, a TLS edge, a
    different public port) would otherwise skew every assertion below.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    for key in ("CREMIND_OAUTH_REDIRECT_URI", "CREMIND_UI_PORT", "CREMIND_TLS_TERMINATION"):
        monkeypatch.delenv(key, raising=False)


def _app_url(monkeypatch, value: str) -> None:
    monkeypatch.setattr(BaseConfig, "APP_URL", value, raising=False)


# ── loopback_http_origin ─────────────────────────────────────────────────────

@pytest.mark.parametrize(("url", "expected"), [
    ("http://localhost:1515", "http://localhost:1515"),
    ("https://localhost:1515", "http://localhost:1515"),
    ("http://127.0.0.1:1112", "http://127.0.0.1:1112"),
    ("https://127.0.0.1:8443", "http://127.0.0.1:8443"),
    # The whole of 127.0.0.0/8 is loopback, not just .1.
    ("http://127.1.2.3:1515", "http://127.1.2.3:1515"),
    ("http://[::1]:1515", "http://[::1]:1515"),
    ("https://[::1]:8443", "http://[::1]:8443"),
    # Default ports are made explicit: the scheme is dropped, the port kept.
    ("https://localhost", "http://localhost:443"),
    ("http://localhost", "http://localhost:80"),
    ("https://[::1]", "http://[::1]:443"),
    # Origins only — trailing slashes, paths, queries, credentials all go.
    ("http://localhost:1515/", "http://localhost:1515"),
    ("https://localhost:1515/api/oauth/callback?x=1#y", "http://localhost:1515"),
    ("http://user:secret@localhost:1515", "http://localhost:1515"),
    ("  HTTP://LOCALHOST:1515  ", "http://localhost:1515"),
])
def test_loopback_urls_map_to_their_http_origin(url, expected):
    assert ol.loopback_http_origin(url) == expected


@pytest.mark.parametrize("url", [
    "https://cremind.example.com",
    "https://cremind.example.com:1515",
    "http://192.168.1.50:1515",
    "http://10.0.0.1:1515",
    "http://0.0.0.0:1112",       # listen-all, not a browser origin
    "http://[::]:1515",
    "http://localhost.example.com:1515",
    "http://127.0.0.1.nip.io:1515",
    "http://[::1%25lo]:1515",    # a zone id is not a usable origin
    "ftp://localhost:21",
    "localhost:1515",            # no scheme
    "http://localhost:not-a-port",
    "http://localhost:99999",
    "http://localhost:0",
    "",
    "   ",
    None,
])
def test_anything_else_is_not_a_loopback_origin(url):
    assert ol.loopback_http_origin(url) is None


# ── step 1: APP_URL ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(("app_url", "expected"), [
    ("http://localhost:1515", "http://localhost:1515"),
    ("http://localhost:1515/", "http://localhost:1515"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("http://[::1]:1515", "http://[::1]:1515"),
    # An https APP_URL maps to http on the SAME port: the same-port TLS listener
    # hands plaintext to the recovery surface, which redirects the callback on.
    ("https://localhost:1515", "http://localhost:1515"),
    ("https://127.1.2.3:8443", "http://127.1.2.3:8443"),
    ("https://[::1]:1515", "http://[::1]:1515"),
    ("https://localhost", "http://localhost:443"),
])
def test_a_loopback_app_url_names_the_redirect_origin(monkeypatch, app_url, expected):
    _app_url(monkeypatch, app_url)
    assert ol.app_url_loopback_origin() == expected
    assert ol.google_loopback_origin(fallback=False) == expected
    assert ol.google_redirect_uri("/api/oauth/callback", fallback=False) == expected + "/api/oauth/callback"


# ── an APP_URL naming the INTERNAL API bind ──────────────────────────────────
#
# PORT (1112) binds 127.0.0.1 only and is never published, so in a container it
# is the container's own loopback — an address the user's browser cannot open.
# Docker installs have been seen with exactly that APP_URL; Google's redirect
# has to go to the public bind instead, which serves the same application.

def test_an_app_url_naming_the_internal_bind_falls_back_to_the_public_port(monkeypatch):
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    _app_url(monkeypatch, "http://localhost:1112")
    assert ol.app_url_loopback_origin() is None
    assert ol.google_loopback_origin(fallback=False) is None
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_the_internal_bind_guard_only_applies_when_a_public_port_exists(monkeypatch):
    """``CREMIND_UI_PORT=0`` serves loopback-only behind an external proxy, and a
    native install's 127.0.0.1 IS the browser's: neither is a wrong address."""
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    _app_url(monkeypatch, "http://localhost:1112")
    assert ol.app_url_loopback_origin() == "http://localhost:1112"

    # The public bind and the internal one being the same port is not a conflict.
    monkeypatch.setenv("CREMIND_UI_PORT", "1112")
    assert ol.app_url_loopback_origin() == "http://localhost:1112"


def test_a_pin_still_wins_over_the_public_port_fallback(monkeypatch):
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    monkeypatch.setattr(BaseConfig, "PORT", 1112, raising=False)
    _app_url(monkeypatch, "http://localhost:1112")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:9090/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:9090"


@pytest.mark.parametrize("app_url", [
    "https://cremind.example.com",
    "http://192.168.1.50:1515",
    "http://0.0.0.0:1112",
    "http://localhost:not-a-port",
    "",
])
def test_a_non_loopback_app_url_names_nothing_without_fallback(monkeypatch, app_url):
    _app_url(monkeypatch, app_url)
    assert ol.app_url_loopback_origin() is None
    assert ol.google_loopback_origin(fallback=False) is None
    assert ol.google_redirect_uri("/api/oauth/callback", fallback=False) is None


def test_https_app_url_behind_an_edge_proxy_is_not_derived(monkeypatch):
    """The proxy owns TLS on that port and will not answer plain HTTP there."""
    _app_url(monkeypatch, "https://localhost:8443")
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    assert ol.app_url_loopback_origin() is None
    assert ol.google_loopback_origin(fallback=False) is None


def test_https_app_url_without_a_public_bind_is_not_derived(monkeypatch):
    """CREMIND_UI_PORT=0: a local reverse proxy in front of the loopback app."""
    _app_url(monkeypatch, "https://localhost:8443")
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    assert ol.app_url_loopback_origin() is None
    assert ol.google_loopback_origin(fallback=False) is None


def test_http_app_url_is_derived_whatever_terminates_tls(monkeypatch):
    """Plain HTTP on a loopback APP_URL is the redirect itself; nothing to bounce."""
    _app_url(monkeypatch, "http://localhost:1515")
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:1515"
    monkeypatch.delenv("CREMIND_TLS_TERMINATION")
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:1515"


# ── step 2: the operator's pin ───────────────────────────────────────────────

def test_a_loopback_app_url_outranks_the_pin(monkeypatch):
    """It is the address the user is already browsing."""
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://127.0.0.1:9999/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:1515"


def test_a_loopback_pin_is_honoured_when_app_url_is_not_loopback(monkeypatch):
    _app_url(monkeypatch, "https://test.cremind.io")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:1515"
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://[::1]:9999/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://[::1]:9999"


def test_an_https_loopback_pin_is_normalised_not_ignored(monkeypatch):
    _app_url(monkeypatch, "https://test.cremind.io")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "https://localhost:8443/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:8443"
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "https://localhost/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://localhost:443"


def test_the_pin_outranks_the_fallback_port(monkeypatch):
    """A port scraped from a LAN APP_URL is a guess; the pin was arranged."""
    _app_url(monkeypatch, "http://192.168.1.50:8080")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_the_pin_covers_an_edge_https_app_url(monkeypatch):
    _app_url(monkeypatch, "https://localhost:8443")
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://127.0.0.1:1515/api/oauth/callback")
    assert ol.google_loopback_origin(fallback=False) == "http://127.0.0.1:1515"


@pytest.mark.parametrize("pin", [
    "https://test.cremind.io/api/oauth/callback",
    "http://192.168.1.50:1515/api/oauth/callback",
    "http://localhost:not-a-port/api/oauth/callback",
    "   ",
])
def test_a_non_loopback_pin_is_ignored(monkeypatch, pin):
    """Honouring it would turn a missed capture into a hard Google error."""
    _app_url(monkeypatch, "https://test.cremind.io")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", pin)
    assert ol.google_loopback_origin(fallback=False) is None
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


# ── step 3: the fallback ─────────────────────────────────────────────────────

def test_fallback_uses_app_url_explicit_port(monkeypatch):
    """A remapped publish (-p 8080:1515) is reachable on APP_URL's port."""
    _app_url(monkeypatch, "http://192.168.1.50:8080")
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:8080"


def test_fallback_for_a_portless_public_app_url_is_never_portless(monkeypatch):
    """``urlsplit("https://host").port`` is None; :80 is a dead redirect everywhere."""
    _app_url(monkeypatch, "https://test.cremind.io")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"
    monkeypatch.setenv("CREMIND_UI_PORT", "8080")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:8080"


@pytest.mark.parametrize("ui_port", ["0", "not-a-port", "", "-5"])
def test_fallback_ignores_an_unusable_public_port(monkeypatch, ui_port):
    _app_url(monkeypatch, "https://test.cremind.io")
    monkeypatch.setenv("CREMIND_UI_PORT", ui_port)
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_fallback_survives_a_malformed_app_url_port(monkeypatch):
    _app_url(monkeypatch, "http://bad-host:not-a-port")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_fallback_never_reuses_an_https_port_that_only_speaks_tls(monkeypatch):
    """Step 1 declined this APP_URL because its port answers only TLS for us
    (the Ingress holds it); the fallback must not advertise plaintext there
    either, or Calendar connect ends on the controller's 400. It lands on the
    public bind / documented port-forward port instead — the one the skills'
    default uses too, so every Google flow agrees."""
    _app_url(monkeypatch, "https://localhost:8443")
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"
    monkeypatch.setenv("CREMIND_UI_PORT", "9000")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:9000"


def test_fallback_skips_an_edge_https_public_port_too(monkeypatch):
    _app_url(monkeypatch, "https://cremind.example.com:8443")
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_fallback_skips_an_https_port_with_no_public_bind(monkeypatch):
    _app_url(monkeypatch, "https://localhost:8443")
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:1515"


def test_fallback_keeps_a_remapped_http_or_self_served_https_port(monkeypatch):
    """A Docker publish (-p 8080:1515) is reachable on APP_URL's port, and an
    https port this process serves itself also answers plaintext."""
    _app_url(monkeypatch, "http://cremind.lan:8080")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:8080"
    _app_url(monkeypatch, "https://cremind.lan:8443")
    assert ol.google_loopback_origin(fallback=True) == "http://localhost:8443"


def test_redirect_uri_joins_the_callback_path(monkeypatch):
    _app_url(monkeypatch, "https://[::1]:1515/")
    assert (ol.google_redirect_uri("/api/oauth/google-drive/callback", fallback=True)
            == "http://[::1]:1515/api/oauth/google-drive/callback")
    _app_url(monkeypatch, "https://cremind.example.com")
    assert (ol.google_redirect_uri("/api/oauth/google-calendar/callback", fallback=True)
            == "http://localhost:1515/api/oauth/google-calendar/callback")
