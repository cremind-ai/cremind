"""OAuth redirect system-var.

The Google skills (gmail/gcalendar/gsheets/gdocs/gdrive) advertise a redirect
DERIVED from APP_URL (or an operator's loopback pin) and gated to ``http``
loopback origins, because the shared Google "Desktop" client rejects real
hostnames and https redirects. When it is omitted each skill applies its own
``http://localhost:1515/api/oauth/callback`` default, finished with
``complete-link`` where that address does not reach the server — so the gate
must stay exact.

Regression guard: this variable was silently orphaned once — the skills kept
reading ``CREMIND_OAUTH_REDIRECT_URI`` while nothing in the app emitted it, which
made the backend-capture link path and ``complete-link`` unreachable.

(Atlassian is a confidential 3LO Web client allowing only ONE exact-match
callback, so its redirect is a fixed value defaulted inside the jira/confluence
skill config rather than a system var — nothing to assert here.)
"""
import pytest

import app.config.system_vars as sv
from app.config.settings import BaseConfig

CALLBACK_PATH = "/api/oauth/callback"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    """The resolver also reads the pin, the TLS edge and the public port."""
    for key in ("CREMIND_OAUTH_REDIRECT_URI", "CREMIND_UI_PORT", "CREMIND_TLS_TERMINATION"):
        monkeypatch.delenv(key, raising=False)


def _spec():
    return next(s for s in sv.SYSTEM_VARS if s.name == "CREMIND_OAUTH_REDIRECT_URI")


def test_registered_in_system_vars():
    """The skills read this var; if it is not emitted the link flow silently degrades."""
    assert _spec().resolve is sv._resolve_google_redirect_uri


def test_emitted_for_loopback_app_url(monkeypatch):
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515" + CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "http://127.0.0.1:8080", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://127.0.0.1:8080" + CALLBACK_PATH

    # Trailing slash must not double up in the joined URL.
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515/", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515" + CALLBACK_PATH


def test_emitted_for_ipv6_loopback_app_url(monkeypatch):
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://[::1]:1515", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://[::1]:1515" + CALLBACK_PATH
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://[::1]:1515", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://[::1]:1515" + CALLBACK_PATH


def test_omitted_for_non_loopback_app_url(monkeypatch):
    # Ingress/domain/LAN host → Google Desktop client rejects it; the skill falls
    # back to its own default and complete-link.
    for app_url in (
        "https://cremind.example.com",
        "http://192.168.1.50:1515",
        "http://0.0.0.0:1112",  # listen-all default is not a browser origin
        "",
    ):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert sv._resolve_google_redirect_uri(None) is None, app_url


def test_https_loopback_app_url_advertises_http_on_the_same_port(monkeypatch):
    """A TLS local install (the installers' default) keeps its redirect.

    Google refuses an https loopback redirect for this client type, so the
    variable names ``http`` on the same host and port; the same-port TLS listener
    answers that plaintext callback by redirecting it to the HTTPS handler. This
    used to be omitted, which pushed every TLS install onto the manual paste.
    """
    for app_url, expected in (
        ("https://localhost:1515", "http://localhost:1515"),
        ("https://127.0.0.1:1515", "http://127.0.0.1:1515"),
        ("https://localhost", "http://localhost:443"),
    ):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert sv._resolve_google_redirect_uri(None) == expected + CALLBACK_PATH, app_url


def test_https_loopback_app_url_behind_an_edge_proxy_is_omitted(monkeypatch):
    """Plaintext on that port reaches the proxy, not the same-port listener."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://localhost:1515", raising=False)
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    assert sv._resolve_google_redirect_uri(None) is None


def test_a_loopback_pin_is_emitted_for_a_public_app_url(monkeypatch):
    """The K8s shape: Ingress APP_URL plus a port-forward the operator pinned."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://test.cremind.io", raising=False)
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515" + CALLBACK_PATH
    # An https loopback pin is normalised to its http origin, not passed through.
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "https://127.0.0.1:8443/api/oauth/callback")
    assert sv._resolve_google_redirect_uri(None) == "http://127.0.0.1:8443" + CALLBACK_PATH


def test_a_non_loopback_pin_is_not_emitted(monkeypatch):
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://test.cremind.io", raising=False)
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "https://test.cremind.io/api/oauth/callback")
    assert sv._resolve_google_redirect_uri(None) is None


def test_build_system_env_includes_and_omits(monkeypatch):
    """End-to-end through the real builder the skills' subprocess env comes from."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    env = sv.build_system_env(None)
    assert env["CREMIND_OAUTH_REDIRECT_URI"] == "http://localhost:1515" + CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "https://localhost:1515", raising=False)
    assert sv.build_system_env(None)["CREMIND_OAUTH_REDIRECT_URI"] == "http://localhost:1515" + CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com", raising=False)
    assert "CREMIND_OAUTH_REDIRECT_URI" not in sv.build_system_env(None)
