"""Plaintext Google OAuth callbacks continue to their HTTPS handlers.

Google's Desktop client only redirects to ``http://<loopback>:<port>``, so on an
HTTPS install the authorization response arrives over plaintext — on the same
port the TLS listener serves, or on a pod behind an edge proxy. The recovery
surface must forward exactly those three GET callbacks to HTTPS with the query
untouched, and keep refusing every other plaintext API with a 426.

What the HTTPS handler finally answers is not pinned here (it navigates the
browser back into the app); only its side effects are.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app.api.oauth_callback import get_oauth_callback_routes
from app.api.tls_recovery import GOOGLE_CALLBACK_PATHS, EdgeTlsRecovery, TlsHandoffCors, recovery_app
from app.config import tls_mode, tls_transition as transition
from app.config.settings import BaseConfig
from app.server import JWTAuthBackend

SECRET = "tls-oauth-callback-recovery-test-secret-32plus"
STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"  # matches the callback handlers' state charset
QUERY = f"code=4%2F0AVG-x_y.z~w&state={STATE}&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar.events"
PATHS = sorted(GOOGLE_CALLBACK_PATHS)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setattr(BaseConfig, "CREMIND_INSTALL_DIR", str(tmp_path / "install"))
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://localhost:1515")
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "")
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "")
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "")
    monkeypatch.setattr(BaseConfig, "SSL_AUTO_HOSTS", [])
    monkeypatch.setattr(BaseConfig, "CORS_ALLOWED_ORIGINS", ["https://localhost:1515"])
    monkeypatch.setattr(BaseConfig, "get_jwt_secret", classmethod(lambda cls: SECRET))
    monkeypatch.setattr(tls_mode, "_boot_serving_https", False)
    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    for key in ("CREMIND_TLS_TERMINATION", "CREMIND_OAUTH_REDIRECT_URI",
                "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED"):
        monkeypatch.delenv(key, raising=False)
    # Nothing here needs a real certificate; never generate one in a test.
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", lambda *_args: ("cert", "key"))
    return tmp_path


@pytest.fixture
def same_port(environment, monkeypatch):
    """This process terminates TLS on its public port (the relay topology)."""
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    return environment


@pytest.fixture
def app():
    """The production middleware order around the real callback routes."""
    return Starlette(routes=get_oauth_callback_routes(), middleware=[
        Middleware(EdgeTlsRecovery),
        Middleware(TlsHandoffCors),
        Middleware(AuthenticationMiddleware, backend=JWTAuthBackend(lambda: SECRET)),
    ])


def _plain(base_url="http://localhost:1515"):
    """What the same-port relay hands plaintext to: the recovery app, directly."""
    return TestClient(recovery_app, base_url=base_url, follow_redirects=False)


def _inbox(system_dir):
    return system_dir / "oauth_inbox" / f"{STATE}.txt"


async def _asgi_get(app, *, path: str, query: bytes, host: bytes = b"localhost:1515", method="GET"):
    """Drive an ASGI app with a hand-built scope: the query stays raw bytes."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": query, "headers": [(b"host", host)],
        "client": ("127.0.0.1", 50000), "server": ("localhost", 1515),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    return start["status"], {key.decode().lower(): value for key, value in start["headers"]}


# ── same-port topology ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
def test_each_google_callback_is_redirected_to_https_on_the_same_port(same_port, path):
    response = _plain().get(f"{path}?{QUERY}")
    assert response.status_code == 307
    assert response.headers["location"] == f"https://localhost:1515{path}?{QUERY}"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["connection"] == "close"
    assert response.content == b""


def test_a_port_forward_keeps_its_own_local_port(same_port):
    """kubectl port-forward 8443:80: the browser's port, not the pod's."""
    response = _plain("http://127.0.0.1:8443").get(f"/api/oauth/callback?{QUERY}")
    assert response.headers["location"] == f"https://127.0.0.1:8443/api/oauth/callback?{QUERY}"
    # TestClient cannot address an IPv6 host, so speak ASGI directly.
    status, headers = asyncio.run(_asgi_get(
        recovery_app, path="/api/oauth/callback", query=QUERY.encode(), host=b"[::1]:8443"))
    assert status == 307
    assert headers["location"] == f"https://[::1]:8443/api/oauth/callback?{QUERY}".encode()


def test_a_top_level_navigation_is_redirected_not_given_the_recovery_page(same_port):
    """Google's redirect is a browser navigation, carrying exactly the headers
    that would otherwise select the recovery document."""
    response = _plain().get(
        f"/api/oauth/google-drive/callback?{QUERY}",
        headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"},
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://localhost:1515/api/oauth/google-drive/callback?")


def test_the_query_is_forwarded_byte_for_byte(same_port):
    """The skills replay this exact query into their token exchange.

    Includes characters a URL-quoting redirect would have rewritten (``|``,
    ``"``), a ``#`` that re-parsing would have cut, and a raw non-ASCII byte a
    UTF-8 round trip could not re-encode into a header.
    """
    query = (b"code=4/0AVG-x_y.z~w&state=" + STATE.encode()
             + b"&scope=email%20openid+profile&hd=a|b&x=\"q\"&frag=#tail&raw=\xe9")
    status, headers = asyncio.run(_asgi_get(recovery_app, path="/api/oauth/callback", query=query))
    assert status == 307
    assert headers["location"] == b"https://localhost:1515/api/oauth/callback?" + query


def test_a_callback_without_a_query_is_still_redirected(same_port):
    response = _plain().get("/api/oauth/callback")
    assert response.status_code == 307
    assert response.headers["location"] == "https://localhost:1515/api/oauth/callback"


def test_following_the_redirect_reaches_the_handler(same_port, app):
    """End to end without sockets: plaintext hop via the recovery app, HTTPS hop
    through the real middleware stack to the real inbox handler."""
    location = _plain().get(f"/api/oauth/callback?{QUERY}").headers["location"]
    response = TestClient(app, follow_redirects=False).get(location)
    assert response.status_code < 400
    assert _inbox(same_port).read_text(encoding="utf-8") == QUERY


def test_an_invalid_host_gets_no_redirect(same_port):
    response = _plain().get(f"/api/oauth/callback?{QUERY}", headers={"Host": "user@example.test"})
    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.headers["connection"] == "close"


# ── edge topology ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
def test_behind_an_edge_proxy_the_callback_continues_on_the_public_origin(environment, app, monkeypatch, path):
    """A port-forward straight to the pod speaks plain HTTP; APP_URL is the
    HTTPS origin the Ingress serves, so that is where the handler runs."""
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com")
    response = TestClient(app, base_url="http://localhost:1515", follow_redirects=False).get(f"{path}?{QUERY}")
    assert response.status_code == 307
    assert response.headers["location"] == f"https://cremind.example.com{path}?{QUERY}"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    # Nothing was captured over plaintext: the handler has not run yet.
    assert not _inbox(environment).exists()


def test_edge_public_origin_keeps_its_port(environment, app, monkeypatch):
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://primary.local:8443/")
    response = TestClient(app, base_url="http://localhost:1515", follow_redirects=False).get(
        f"/api/oauth/callback?{QUERY}")
    assert response.headers["location"] == f"https://primary.local:8443/api/oauth/callback?{QUERY}"


def test_after_a_switch_the_middleware_path_redirects_too(same_port, app):
    """Once the boundary has moved, EdgeTlsRecovery hands public plaintext to
    the recovery app as well; the callback must get the same treatment there."""
    transition.mark_active()
    assert transition.load_transition()["phase"] == "active"
    client = TestClient(app, base_url="http://localhost:1515", follow_redirects=False)
    response = client.get(f"/api/oauth/google-calendar/callback?{QUERY}")
    assert response.status_code == 307
    assert response.headers["location"] == f"https://localhost:1515/api/oauth/google-calendar/callback?{QUERY}"
    assert client.get("/api/tls/status").status_code == 426


# ── what stays closed ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "OPTIONS"])
@pytest.mark.parametrize("path", PATHS)
def test_only_get_is_redirected(same_port, method, path):
    response = _plain().request(method, f"{path}?{QUERY}")
    assert response.status_code == 426
    assert "location" not in response.headers
    assert response.headers["connection"] == "close"


@pytest.mark.parametrize("path", [
    "/api/oauth/a2a/callback",          # A2A tool auth has its own redirect
    "/api/oauth/callbackX",
    "/api/oauth/callback/",
    "/api/oauth/callback/extra",
    "/api/oauth/google-calendar/callback/x",
    "/api/me",
    "/api/tls/handoff",
])
def test_every_other_plaintext_api_still_gets_426(same_port, path):
    response = _plain().get(f"{path}?{QUERY}", headers={"Accept": "text/html"})
    assert response.status_code == 426
    assert "location" not in response.headers


def test_the_match_is_exact_not_case_folded(same_port):
    """``/API/...`` is no API path to the recovery surface — just another
    navigation, answered by the recovery page — and never a redirect."""
    response = _plain().get(f"/API/oauth/callback?{QUERY}", headers={"Accept": "text/html"})
    assert response.status_code != 307
    assert "location" not in response.headers


def test_edge_non_callback_api_still_gets_426(environment, app, monkeypatch):
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com")
    client = TestClient(app, base_url="http://localhost:1515", follow_redirects=False)
    assert client.get(f"/api/oauth/a2a/callback?{QUERY}").status_code == 426
    assert client.post(f"/api/oauth/callback?{QUERY}").status_code == 426


def test_navigation_still_gets_the_unchanged_recovery_document(same_port):
    page = _plain().get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "Opening Cremind over HTTPS" in page.text
    assert "location" not in page.headers
    script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in page.headers["content-security-policy"]
    assert "connect-src https:" in page.headers["content-security-policy"]
