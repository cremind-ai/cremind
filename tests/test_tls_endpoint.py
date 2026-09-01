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
    """The file route takes no filename, so nothing else in tls/ is addressable."""
    ensure_local_tls(str(system_dir))

    paths = [route.path for route in get_tls_routes()]
    assert paths == ["/ca.pem", "/api/tls/trust"]
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


# ── POST /api/tls/trust — the one-click "Trust it on this device" ─────────
#
# The endpoint writes a root CA into an OS trust store, so what these tests
# pin is the guard stack: native-only, loopback-only, fingerprint echo
# required, honest fallbacks when the tool fails. The actual per-OS command
# construction is tested in tests/config/test_tls_trust.py; here every
# execution is stubbed.


class _ForceClient:
    """ASGI wrapper that sets the peer address the guards will see.

    Starlette's TestClient reports the peer as ``("testclient", 50000)``,
    which parses as no IP at all — every request would fail the loopback
    check for the wrong reason.
    """

    def __init__(self, app, host: str):
        self._app = app
        self._host = host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self._host, 40000)
        await self._app(scope, receive, send)


def _trust_client(client_host: str = "127.0.0.1") -> TestClient:
    app = Starlette(
        routes=get_tls_routes(),
        middleware=[Middleware(AuthenticationMiddleware, backend=_AlwaysAnon())],
    )
    return TestClient(_ForceClient(app, client_host))


@pytest.fixture
def native_env(monkeypatch: pytest.MonkeyPatch):
    """A native install, pre-setup — the wizard's situation."""
    from types import SimpleNamespace

    import app.runtime as runtime

    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: SimpleNamespace(
            storage_ready=False,
            config_storage=SimpleNamespace(is_setup_complete=lambda: False),
        ),
    )


@pytest.fixture
def post_setup_env(monkeypatch: pytest.MonkeyPatch):
    """A native install whose setup is finished — Settings' situation.

    Settings → HTTPS & Certificate offers the same one-click trust long after
    the wizard is gone, so the admin gate is the guard that matters here; in
    ``native_env`` it never runs.
    """
    from types import SimpleNamespace

    import app.runtime as runtime

    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: SimpleNamespace(
            storage_ready=True,
            config_storage=SimpleNamespace(is_setup_complete=lambda: True),
        ),
    )


def _fingerprint(system_dir) -> str:
    from app.config.tls_auto import ca_fingerprint_sha256

    fp = ca_fingerprint_sha256(str(system_dir))
    assert fp is not None
    return fp


def _stub_plan(monkeypatch, *, supported=True, run_ok=True, run_error=None,
               already=False):
    import app.api.tls as tls_api
    from app.config.tls_trust import TrustPlan

    plan = TrustPlan(
        supported=supported,
        store="test store" if supported else None,
        commands=[["certutil", "-addstore", "-user", "Root", "ca.pem"]],
        reason=None if supported else "not on this platform",
        os_prompt="windows" if supported else None,
    )
    calls: list[TrustPlan] = []

    def _run(p):
        calls.append(p)
        return (run_ok, run_error)

    monkeypatch.setattr(tls_api, "server_trust_plan", lambda _p: plan)
    monkeypatch.setattr(tls_api, "run_trust_plan", _run)
    monkeypatch.setattr(tls_api, "already_trusted", lambda _p: already)
    return calls


def test_trust_happy_path_runs_the_plan(system_dir, native_env, monkeypatch):
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["trusted"] is True and body["already_trusted"] is False
    assert body["store"] == "test store"
    assert len(calls) == 1


def test_trust_is_refused_for_a_remote_client(system_dir, native_env, monkeypatch):
    """Trusting server-side lands on the SERVER's machine — a remote browser
    must be told to run the command on its own device instead."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)

    response = _trust_client("192.168.1.20").post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 403
    assert calls == []


def test_trust_is_refused_in_containers(system_dir, native_env, monkeypatch):
    """Docker/Kubernetes can only write the container's store, which the
    browser on the host never consults."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)

    for mode in ("docker", "kubernetes"):
        monkeypatch.setenv("INSTALL_MODE", mode)
        response = _trust_client().post(
            "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
        )
        assert response.status_code == 409, mode
    assert calls == []


def test_trust_requires_the_fingerprint_echo(system_dir, native_env, monkeypatch):
    """The echo proves the caller read the CA from the same origin (a
    cross-site page cannot) and pins the request to THIS CA."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)
    client = _trust_client()

    assert client.post("/api/tls/trust", json={}).status_code == 400
    assert client.post(
        "/api/tls/trust", content=b"ca_sha256=x",
        headers={"content-type": "application/x-www-form-urlencoded"},
    ).status_code == 400
    wrong = "AA:" * 31 + "AA"
    assert client.post(
        "/api/tls/trust", json={"ca_sha256": wrong}
    ).status_code == 409
    assert calls == []


def test_trust_404s_without_a_ca(system_dir, native_env, monkeypatch):
    _stub_plan(monkeypatch)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": "AA:BB"}
    )

    assert response.status_code == 404


def test_trust_reports_an_unsupported_platform_with_the_manual_commands(
    system_dir, native_env, monkeypatch,
):
    ensure_local_tls(str(system_dir))
    _stub_plan(monkeypatch, supported=False)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 409
    body = response.json()
    assert body["trusted"] is False
    assert body["manual_commands"], "the fallback must be actionable"


def test_trust_reports_a_tool_failure_honestly(system_dir, native_env, monkeypatch):
    """A cancelled Windows dialog comes back as a tool failure — the
    response must say so and hand over the manual command, never claim
    success."""
    ensure_local_tls(str(system_dir))
    _stub_plan(monkeypatch, run_ok=False, run_error="certutil exited with 1")

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["trusted"] is False
    assert "certutil" in body["error"]
    assert body["manual_commands"]


def test_trust_skips_the_dialog_when_already_trusted(
    system_dir, native_env, monkeypatch,
):
    """A wizard re-run must not pop the OS confirmation again."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch, already=True)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 200
    assert response.json()["already_trusted"] is True
    assert calls == []


def test_trust_needs_the_admin_token_once_setup_is_complete(
    system_dir, post_setup_env, monkeypatch,
):
    """The pre-setup window is open by design; afterwards, writing a root CA
    into the machine's trust store is admin-only. The refusal must come
    before anything touches the trust store."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code in (401, 403)
    assert calls == []


def test_trust_works_post_setup_for_an_admin(
    system_dir, post_setup_env, monkeypatch,
):
    """Settings → HTTPS & Certificate: admin + loopback + fingerprint still
    compose into a working trust, so a user who skipped the wizard step can
    finish it later."""
    ensure_local_tls(str(system_dir))
    calls = _stub_plan(monkeypatch)
    # ``post_trust`` imports require_admin inside the function, so the patch
    # has to land on the defining module.
    monkeypatch.setattr("app.api._auth.require_admin", lambda _r: None)

    response = _trust_client().post(
        "/api/tls/trust", json={"ca_sha256": _fingerprint(system_dir)}
    )

    assert response.status_code == 200
    assert response.json()["trusted"] is True
    assert len(calls) == 1
