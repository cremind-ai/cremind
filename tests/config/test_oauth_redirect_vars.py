"""OAuth redirect system-var.

The Google skills (gmail/gcalendar/gsheets/gdocs/gdrive) advertise a redirect
DERIVED from APP_URL and gated to loopback origins, because the shared Google
"Desktop" client rejects real hostnames. When it is omitted the skill falls back
to the manual ``complete-link`` paste, so the gate must stay exact.

Regression guard: this variable was silently orphaned once — the skills kept
reading ``CREMIND_OAUTH_REDIRECT_URI`` while nothing in the app emitted it, which
made the backend-capture link path and ``complete-link`` unreachable.

(Atlassian is a confidential 3LO Web client allowing only ONE exact-match
callback, so its redirect is a fixed value defaulted inside the jira/confluence
skill config rather than a system var — nothing to assert here.)
"""
import app.config.system_vars as sv
from app.config.settings import BaseConfig

CALLBACK_PATH = "/api/oauth/callback"


def _spec():
    return next(s for s in sv.SYSTEM_VARS if s.name == "CREMIND_OAUTH_REDIRECT_URI")


def test_registered_in_system_vars():
    """The skills read this var; if it is not emitted the link flow silently degrades."""
    assert _spec().resolve is sv._resolve_google_redirect_uri


def test_emitted_for_loopback_app_url(monkeypatch):
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515" + CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "http://127.0.0.1:1112", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://127.0.0.1:1112" + CALLBACK_PATH

    # Trailing slash must not double up in the joined URL.
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515/", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515" + CALLBACK_PATH


def test_omitted_for_non_loopback_app_url(monkeypatch):
    # Ingress/domain/LAN host → Google Desktop client rejects it; use manual paste.
    for app_url in (
        "https://cremind.example.com",
        "http://192.168.1.50:1515",
        "http://0.0.0.0:1112",  # listen-all default is not a browser origin
        "",
    ):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert sv._resolve_google_redirect_uri(None) is None, app_url


def test_omitted_for_https_loopback_app_url(monkeypatch):
    """A TLS local install (CREMIND_SSL) is loopback but still not usable here.

    The installed-app loopback flow is http-only, so an https redirect would be
    refused at the consent screen. Omitting it falls back to manual paste — the
    same path a non-loopback APP_URL takes. Since the installers default local
    installs to TLS, this is the common case, not an exotic one.
    """
    for app_url in ("https://localhost:1515", "https://127.0.0.1:1515"):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert sv._resolve_google_redirect_uri(None) is None, app_url


def test_build_system_env_includes_and_omits(monkeypatch):
    """End-to-end through the real builder the skills' subprocess env comes from."""
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    env = sv.build_system_env(None)
    assert env["CREMIND_OAUTH_REDIRECT_URI"] == "http://localhost:1515" + CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com", raising=False)
    assert "CREMIND_OAUTH_REDIRECT_URI" not in sv.build_system_env(None)
