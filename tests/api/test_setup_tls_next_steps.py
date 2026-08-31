"""The HTTPS hand-off fields the Setup Wizard finishes on.

``_setup_tls_next_steps`` tells the wizard three things at the moment setup
completes: whether a switch to HTTPS is coming, where to send the browser, and
whether it may restart the server itself. Tested at the seam rather than
through the whole setup handler, the way ``_kubernetes_sqlite_rejection`` is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.config import _setup_tls_next_steps
from app.config import tls_mode
from app.config.settings import BaseConfig


def _request(host: str | None = "localhost:1515"):
    return SimpleNamespace(headers={"host": host} if host else {})


@pytest.fixture(autouse=True)
def _tls_env(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "after-setup", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example", raising=False)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.delenv("CREMIND_UI_PORT", raising=False)
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    tls_mode.record_boot_tls(False)


def test_reports_the_pending_switch():
    result = _setup_tls_next_steps(_request())
    assert result["tls_pending"] is True
    assert result["restart_supported"] is True


def test_next_origin_follows_the_host_the_browser_used():
    """Behind `kubectl port-forward` the browser reaches localhost, which is
    not what APP_URL says — the client's own Host header is the only name
    known to work from where the browser is sitting."""
    assert _setup_tls_next_steps(_request("localhost:1515"))["next_origin"] == (
        "https://localhost:1515"
    )


def test_next_origin_falls_back_to_app_url_without_a_host_header():
    assert _setup_tls_next_steps(_request(None))["next_origin"] == (
        "https://cremind.example"
    )


def test_nothing_pending_means_no_origin(monkeypatch):
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "", raising=False)
    result = _setup_tls_next_steps(_request())
    assert result["tls_pending"] is False
    assert result["next_origin"] is None


def test_already_serving_https_is_not_pending():
    """A post-restart profile setup must not try to pivot again."""
    tls_mode.record_boot_tls(True)
    assert _setup_tls_next_steps(_request())["tls_pending"] is False


def test_an_unsupervised_install_reports_it_cannot_restart(monkeypatch):
    """The wizard asks the operator instead of killing their server."""
    monkeypatch.setenv("INSTALL_MODE", "native")
    result = _setup_tls_next_steps(_request())
    assert result["tls_pending"] is True
    assert result["restart_supported"] is False
