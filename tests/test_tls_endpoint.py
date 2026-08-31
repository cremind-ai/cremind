"""Tests for ``GET /ca.pem`` — the local CA download.

Two properties matter here and nothing else really does. It has to answer
**without a token**, because the client that needs it is a browser sitting on a
certificate warning, before login and possibly before the Setup Wizard has run.
And it must serve the CA certificate *only* — ``ca.key``, ``cert.pem`` and
``key.pem`` sit in the same directory, so this is the one route where a
parameterised path would be a private-key disclosure.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app.api.tls import get_tls_routes
from app.config.tls_auto import ensure_local_tls


class _AlwaysAnon(AuthenticationBackend):
    """Test backend that never authenticates — the browser's situation."""

    async def authenticate(self, conn):
        return AuthCredentials([]), None


@pytest.fixture
def system_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at an empty system dir. No CA exists yet."""
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    app = Starlette(
        routes=get_tls_routes(),
        middleware=[Middleware(AuthenticationMiddleware, backend=_AlwaysAnon())],
    )
    return TestClient(app)


def test_serves_the_ca_without_authentication(system_dir, client):
    ensure_local_tls(str(system_dir))

    response = client.get("/ca.pem")

    assert response.status_code == 200
    assert response.text.startswith("-----BEGIN CERTIFICATE-----")
    assert response.text.strip().endswith("-----END CERTIFICATE-----")


def test_the_body_is_exactly_the_ca_file(system_dir, client):
    ensure_local_tls(str(system_dir))
    on_disk = (system_dir / "tls" / "ca.pem").read_bytes()

    assert client.get("/ca.pem").content == on_disk


def test_never_serves_private_key_material(system_dir, client):
    """The CA private key lives next to the certificate; it must not leak."""
    ensure_local_tls(str(system_dir))

    body = client.get("/ca.pem").text

    assert "PRIVATE KEY" not in body
    # And the certificate served is the CA's, not the server leaf's.
    leaf = (system_dir / "tls" / "cert.pem").read_text()
    assert body.strip() != leaf.strip()


def test_no_sibling_file_is_reachable(system_dir, client):
    """The route takes no filename, so nothing else in tls/ is addressable."""
    ensure_local_tls(str(system_dir))

    paths = [route.path for route in get_tls_routes()]
    assert paths == ["/ca.pem"]
    # There is no route that could resolve these, with or without traversal.
    for probe in ("/ca.key", "/tls/ca.key", "/key.pem", "/ca.pem/../ca.key"):
        assert client.get(probe).status_code == 404


def test_downloads_as_a_named_file(system_dir, client):
    ensure_local_tls(str(system_dir))

    response = client.get("/ca.pem")

    assert response.headers["content-type"] == "application/x-pem-file"
    assert "attachment" in response.headers["content-disposition"]
    assert "cremind-local-ca.pem" in response.headers["content-disposition"]
    # A regenerated CA must not be masked by a cached copy.
    assert response.headers["cache-control"] == "no-cache"


def test_404_when_no_ca_has_been_generated(system_dir, client):
    """TLS off, or an operator-supplied certificate pair: no local CA exists."""
    response = client.get("/ca.pem")

    assert response.status_code == 404
    # Both CA-generating modes are named: under after-setup this endpoint is
    # live during the plain-HTTP wizard phase, so a 404 there must not send
    # the reader off to look for the wrong setting.
    error = response.json()["error"]
    assert "auto" in error and "after-setup" in error


def test_head_is_allowed(system_dir, client):
    ensure_local_tls(str(system_dir))

    assert client.head("/ca.pem").status_code == 200


def test_follows_a_relocated_system_dir(system_dir, client, tmp_path, monkeypatch):
    """The wizard can move the system dir at runtime; the path is per-request."""
    from app.config.settings import BaseConfig

    assert client.get("/ca.pem").status_code == 404

    moved = tmp_path / "relocated"
    moved.mkdir()
    ensure_local_tls(str(moved))
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(moved))

    assert client.get("/ca.pem").status_code == 200
