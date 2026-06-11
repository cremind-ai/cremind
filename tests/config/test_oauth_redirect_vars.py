"""OAuth redirect system-vars.

Google (gmail/gcalendar) advertises a redirect DERIVED from APP_URL, gated to
loopback origins (its Desktop client rejects real hostnames). Atlassian
(jira/confluence) is a confidential 3LO Web client that allows only ONE
exact-match callback per app, so its redirect is a single FIXED value
(``CREMIND_ATLASSIAN_REDIRECT_URI``) independent of APP_URL — Option B.
"""
import app.config.system_vars as sv
from app.config.settings import BaseConfig


def _atlassian_resolve():
    return next(s for s in sv.SYSTEM_VARS if s.name == "CREMIND_ATLASSIAN_REDIRECT_URI").resolve


def test_google_redirect_loopback_gated(monkeypatch):
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://localhost:1515/api/oauth/google/callback"
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://127.0.0.1:1112", raising=False)
    assert sv._resolve_google_redirect_uri(None) == "http://127.0.0.1:1112/api/oauth/google/callback"
    # Non-loopback (Ingress/domain/LAN) → omitted so the skill uses manual paste.
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com", raising=False)
    assert sv._resolve_google_redirect_uri(None) is None
    # The 0.0.0.0 listen-all default is not a browser origin.
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://0.0.0.0:1112", raising=False)
    assert sv._resolve_google_redirect_uri(None) is None


def test_atlassian_redirect_is_fixed_independent_of_app_url(monkeypatch):
    fixed = "http://localhost:1515/api/oauth/atlassian/callback"
    monkeypatch.setattr(BaseConfig, "CREMIND_ATLASSIAN_REDIRECT_URI", fixed, raising=False)
    resolve = _atlassian_resolve()
    for app_url in (
        "http://localhost:1515",
        "https://cremind.example.com",
        "http://localhost:1112",
        "http://0.0.0.0:1112",
        "",
    ):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert resolve(None) == fixed  # never varies with APP_URL


def test_atlassian_redirect_override(monkeypatch):
    custom = "https://cremind.example.com/api/oauth/atlassian/callback"
    monkeypatch.setattr(BaseConfig, "CREMIND_ATLASSIAN_REDIRECT_URI", custom, raising=False)
    assert _atlassian_resolve()(None) == custom


def test_atlassian_redirect_empty_omitted(monkeypatch):
    monkeypatch.setattr(BaseConfig, "CREMIND_ATLASSIAN_REDIRECT_URI", "", raising=False)
    assert _atlassian_resolve()(None) is None
