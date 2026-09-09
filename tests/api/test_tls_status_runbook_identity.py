"""Settings shows the real cluster names only if the SPA asks as the admin.

Two halves of the same feature, built one after the other, disagree by default:

- the Kubernetes HTTPS runbook prints the real namespace, Helm release and
  Deployment/Service, so an operator no longer has to look them up and
  substitute them into every command by hand;
- ``GET /api/tls/status`` then had to stop handing that topology to anonymous
  callers. The route must keep answering without a token -- the plaintext
  recovery page and the pre-sign-in wizard both poll it -- so the gate lives in
  the *payload* and keys off ``is_admin``.

Both are right, and together they mean the runbook is only ever as specific as
the caller. ``fetchTlsStatus`` in the SPA sent no ``Authorization`` header at
all, so Settings -> HTTPS & Certificate -- the one screen where an admin reads
this runbook -- rendered exactly the placeholder version the feature exists to
remove.

So this pins both ends of the wire: the handler still discloses the identity to
an admin and to nobody else, and the SPA actually sends the credential it is
holding. The SPA half is asserted against its source text because the UI has no
JavaScript test runner in this repo (see
``tests/install/test_installer_ssl.py::test_generated_cmd_shim_matches_electron_regex``
for the same cross-language pinning).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import jwt
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app.api.tls import get_tls_routes
from app.config import runtime_env, tls_clients, tls_mode
from app.config.settings import BaseConfig
from app.server import JWTAuthBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_API = REPO_ROOT / "ui" / "src" / "services" / "configApi.ts"
SECURITY_SETTINGS = REPO_ROOT / "ui" / "src" / "views" / "SecuritySettings.vue"

SECRET = "tls-status-identity-test-secret-32plus"


# -- the server half -------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    """A Kubernetes pod that knows exactly which cluster object it is.

    Every ambient source of an identity is cut off first (the chart's four
    variables, the two the kubelet injects, and the mounted service-account
    namespace file), so a suite running *inside* a cluster asserts against the
    names below rather than that cluster's own.
    """
    tls_clients.clear()
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setattr(BaseConfig, "CREMIND_INSTALL_DIR", str(tmp_path / "install"))
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://testserver")
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "")
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "")
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "")
    monkeypatch.setattr(BaseConfig, "SSL_AUTO_HOSTS", [])
    monkeypatch.setattr(BaseConfig, "CORS_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(BaseConfig, "get_jwt_secret", classmethod(lambda cls: SECRET))
    monkeypatch.setattr(tls_mode, "_boot_serving_https", False)
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    for key in ("CREMIND_SSL", "APP_URL", "CORS_ALLOWED_ORIGINS", "CREMIND_SSL_AUTO_HOSTS"):
        monkeypatch.setenv(key, "")
    for key in ("CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED", "CREMIND_TLS_TERMINATION",
                "CREMIND_ATLASSIAN_REDIRECT_URI", "CREMIND_K8S_SERVICE_PORT",
                "KUBERNETES_SERVICE_HOST", "HOSTNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime_env, "_SA_NAMESPACE_FILE", tmp_path / "no-such-namespace")
    monkeypatch.setattr("app.auth.tokens.serial_matches", lambda claims: claims.get("tsr") == 3)
    # Real certificate generation has its own tests; this one is about who is
    # allowed to read the runbook.
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", lambda *_args: ("cert", "key"))
    monkeypatch.setattr("app.api.tls.local_trust_capabilities", lambda request: {"supported": False})
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    monkeypatch.setenv("CREMIND_K8S_NAMESPACE", "lee-cremind")
    monkeypatch.setenv("CREMIND_K8S_RELEASE", "cremind-prod")
    monkeypatch.setenv("CREMIND_K8S_WORKLOAD", "cremind-prod")
    app = Starlette(routes=get_tls_routes(), middleware=[
        Middleware(AuthenticationMiddleware, backend=JWTAuthBackend(lambda: SECRET)),
    ])
    return TestClient(app)


def auth(profile: str = "admin") -> dict[str, str]:
    from app.auth.tokens import current_transport_epoch
    token = jwt.encode({"sub": profile, "profile": profile, "tsr": 3,
                        "tep": current_transport_epoch(),
                        "iat": int(time.time()) - 1,
                        "exp": int(time.time()) + 3600}, SECRET)
    return {"Authorization": f"Bearer {token}"}


def test_the_admins_runbook_names_the_real_cluster_objects(client):
    """What Settings is supposed to render once the SPA sends its token."""
    payload = client.get("/api/tls/status", headers=auth()).json()

    assert payload["kubernetes"]["namespace"] == "lee-cremind"
    assert payload["kubernetes"]["release"] == "cremind-prod"
    assert payload["kubernetes"]["workload"] == "cremind-prod"
    assert payload["kubernetes"]["source"] == "chart"
    instructions = " ".join(payload["instructions"])
    assert "lee-cremind" in instructions and "cremind-prod" in instructions
    # Nothing is left for the operator to look up and substitute.
    assert "<namespace>" not in instructions and "<release>" not in instructions
    assert "helm list --all-namespaces" not in payload["instructions"]


def test_an_anonymous_poll_still_answers_and_still_says_nothing(client):
    """The pre-sign-in callers keep working, and learn no cluster topology."""
    payload = client.get("/api/tls/status").json()

    assert payload["kubernetes"] is None
    body = json.dumps(payload)
    assert "lee-cremind" not in body and "cremind-prod" not in body
    # The placeholder runbook an older chart already produces, unchanged.
    assert "helm list --all-namespaces" in payload["instructions"]
    assert "<namespace>" in " ".join(payload["instructions"])
    assert payload["serving_https"] is False


# -- the browser half ------------------------------------------------------

def _fetch_tls_status_source() -> str:
    """The body of ``fetchTlsStatus``, up to its closing brace."""
    text = CONFIG_API.read_text(encoding="utf-8")
    start = text.index("export function fetchTlsStatus(")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_spa_sends_the_admin_token_it_holds_to_tls_status():
    """Without this the Settings page reads the anonymous payload above.

    The token stays optional: the same function polls the *target* origin
    during the HTTPS handoff and the recovery page polls before sign-in, and
    neither has a valid credential to send.
    """
    source = " ".join(_fetch_tls_status_source().split())

    assert re.search(r"fetchTlsStatus\(\s*agentUrl: string,\s*token\?: string,?\s*\)", source), (
        "fetchTlsStatus must take an optional token so no existing caller breaks"
    )
    assert "Bearer ${token}" in source, "fetchTlsStatus never sends the credential"
    # Guarded, so a token-less poll stays a header-free (and CORS-simple) GET.
    assert "token ?" in source or "if (token)" in source


def test_the_settings_page_passes_its_token_at_every_status_poll():
    """Both polls: the initial load, and the refresh after a failed activation."""
    calls = re.findall(r"fetchTlsStatus\(([^)]*)\)", SECURITY_SETTINGS.read_text(encoding="utf-8"))

    assert calls, "SecuritySettings no longer polls /api/tls/status"
    for arguments in calls:
        assert "authToken" in arguments, (
            f"fetchTlsStatus({arguments}) asks anonymously, so the admin is shown "
            "the placeholder Kubernetes runbook"
        )
