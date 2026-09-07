"""HTTPS activation, cross-profile isolation and restart-safe session handoffs."""
import base64
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app.api.tls import get_tls_routes
from app.api.tls_recovery import EdgeTlsRecovery, TlsHandoffCors, recovery_app
from app.config.settings import BaseConfig
from app.config.tls_steps import CHART_REFERENCE, running_chart_version
from app.config import tls_clients, tls_mode, tls_transition as transition
from app.config.tls_auto import ensure_local_tls as real_ensure_local_tls
from app.server import JWTAuthBackend

SECRET = "tls-transition-test-secret-only-32plus"
# The runbook pins the chart that shipped the build answering the request, so
# the expected command moves with app/__version__.py rather than being frozen.
CHART_VERSION = running_chart_version()


@pytest.fixture
def environment(monkeypatch, tmp_path):
    tls_clients.clear()
    monkeypatch.setattr("app.api.tls.QUIESCE_ENROLLMENT_SECONDS", 0.0)
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
    monkeypatch.setenv("INSTALL_MODE", "native")
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    for key in ("CREMIND_SSL", "APP_URL", "CORS_ALLOWED_ORIGINS", "CREMIND_SSL_AUTO_HOSTS"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("CREMIND_ATLASSIAN_REDIRECT_URI", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)
    monkeypatch.delenv("CREMIND_TLS_TERMINATION", raising=False)
    monkeypatch.setattr("app.auth.tokens.serial_matches", lambda claims: claims.get("tsr") == 3)
    # Certificate generation itself has a separate real-files test below.
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", lambda *_args: ("cert", "key"))
    monkeypatch.setattr("app.api.tls.local_trust_capabilities", lambda request: {"supported": False})
    return tmp_path


@pytest.fixture
def client(environment):
    app = Starlette(routes=get_tls_routes(), middleware=[
        Middleware(EdgeTlsRecovery),
        Middleware(TlsHandoffCors),
        Middleware(AuthenticationMiddleware, backend=JWTAuthBackend(lambda: SECRET)),
    ])
    return TestClient(app)


def token(profile="admin", serial=3, expires=None, epoch=None):
    from app.auth.tokens import current_transport_epoch
    if epoch is None:
        epoch = current_transport_epoch()
    return jwt.encode({"sub": profile, "profile": profile, "tsr": serial, "tep": epoch,
                       "iat": int(time.time()) - 1, "exp": expires or int(time.time()) + 3600}, SECRET)


def auth(profile="admin"):
    return {"Authorization": f"Bearer {token(profile)}"}


def prepared(client):
    response = client.post("/api/tls/prepare", json={}, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()["transition"]


def storage_ready(monkeypatch):
    """Let the credential boundary move: advancing it re-signs the token files.

    That needs the JWT secret and the profile serials, both of which come from
    the database — so a switch waits rather than silently skipping every rotated
    profile's credential.
    """
    from app import runtime
    monkeypatch.setattr(runtime.get_state(), "storage_ready", True)
    monkeypatch.setattr("app.auth.serial.all_serials", lambda **_kwargs: {"admin": 3, "alice": 3})


def served_https(monkeypatch):
    """Simulate this process genuinely answering HTTPS, with storage available.

    Both halves matter: the boundary moves when a listener really serves TLS,
    and only then if the token files can be re-signed.
    """
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    storage_ready(monkeypatch)


def ticket_body(value):
    return {"transition_id": value["id"], "source_origin": "http://testserver",
            "target_origin": "https://testserver:80", "route": "/alice/c/conversation?tab=files",
            "state": {"mount": "/electron-renderer/", "drafts": {"cremind:draft:alice:conversation": "unsent"}, "preferences": {}}}


def test_admin_only_prepare_and_default_http(client, environment):
    status = client.get("/api/tls/status").json()
    assert status["serving_https"] is False and status["transition"] is None
    assert not (environment / "tls" / "ca.pem").exists()
    assert client.post("/api/tls/prepare", json={}).status_code == 401
    assert client.post("/api/tls/prepare", json={}, headers=auth("alice")).status_code == 403
    value = prepared(client)
    assert value["phase"] == "prepared"
    assert not (environment / ".env").exists()
    for endpoint in ("activate", "cancel"):
        payload = {"transition_id": value["id"]}
        assert client.post(f"/api/tls/{endpoint}", json=payload).status_code == 401
        assert client.post(
            f"/api/tls/{endpoint}", json=payload, headers=auth("alice"),
        ).status_code == 403


def test_activate_atomically_persists_native_settings_and_schedules_restart(client, environment, monkeypatch):
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    scheduled = []
    monkeypatch.setattr("app.api.system.schedule_system_restart", lambda: scheduled.append(True) or 123)
    (environment / ".env").write_text("KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n", encoding="utf-8")
    value = prepared(client)
    transition.register_source(transition.load_transition(), "http://cremind.lan:1515")
    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    assert result.status_code == 202
    assert result.json()["restart_required"] is False
    assert result.json()["restart_scheduled"] is True
    assert scheduled == [True]
    content = (environment / ".env").read_text()
    assert "KEEP_ME=one" in content and "CREMIND_SSL=true" in content
    assert content.count("CREMIND_SSL=") == 1 and "APP_URL=https://testserver:80" in content
    assert "http://testserver,https://testserver:80" in content
    assert "http://cremind.lan:1515,https://cremind.lan:1515" in content
    assert "CREMIND_ATLASSIAN_REDIRECT_URI=https://testserver:80/api/oauth/callback" in content
    assert transition.load_transition()["phase"] == "activating"
    # A restart is already armed and the supervisor stops consulting the
    # transition file once it has seen this phase, so a cancel accepted now
    # would be undone underneath it.
    armed = client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert armed.status_code == 409 and "already scheduled" in armed.json()["error"]
    assert client.get("/api/tls/status").json()["can_cancel"] is False

    # A restart that never lands must not strand the operator on a switch that
    # can neither complete nor be called off.
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    assert client.get("/api/tls/status").json()["can_cancel"] is True
    cancelled = client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert cancelled.status_code == 200
    assert cancelled.json()["transition"]["phase"] == "cancelled"
    assert cancelled.json()["revert_error"] is None
    # Cancelling puts the native settings back in full, not just the phase.
    assert (environment / ".env").read_text() == "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n"
    assert BaseConfig.APP_URL == "http://testserver"
    assert not (environment / "tls" / "native-rollback.json").exists()


def test_activation_quiesces_registered_tabs_before_changing_token_epoch(
    client, environment, monkeypatch,
):
    from app.auth.tokens import verify_token

    value = prepared(client)
    tab_id = "alice-browser-tab-001"
    alice_token = token("alice")
    registered = client.post(
        "/api/tls/client",
        json={"tab_id": tab_id},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert registered.status_code == 200

    first = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"]},
        headers=auth(),
    )
    assert first.status_code == 202
    assert first.json()["transition"]["phase"] == "quiescing"
    assert first.json()["quiesce_pending"] == 1
    assert verify_token(alice_token) is not None
    assert not (environment / ".env").exists()
    assert tab_id not in json.dumps(first.json())

    wrong_profile = client.post(
        "/api/tls/ready",
        json={"tab_id": tab_id, "transition_id": value["id"]},
        headers=auth(),
    )
    assert wrong_profile.status_code == 403

    for _ in range(2):
        ready = client.post(
            "/api/tls/ready",
            json={"tab_id": tab_id, "transition_id": value["id"]},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert ready.status_code == 200
        assert ready.json()["quiesce_pending"] == 0

    second = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"]},
        headers=auth(),
    )
    assert second.status_code == 202
    assert second.json()["transition"]["phase"] == "activating"
    # The boundary moves with the transport, not with the click: while the
    # deployment change is outstanding this tab's session has to keep working.
    assert verify_token(alice_token) is not None
    served_https(monkeypatch)
    transition.mark_active()
    assert verify_token(alice_token) is None


def test_quiesce_tab_id_cannot_be_reassigned_across_profiles(client):
    tab_id = "shared-browser-tab-001"
    assert client.post(
        "/api/tls/client", json={"tab_id": tab_id}, headers=auth("alice"),
    ).status_code == 200

    conflict = client.post(
        "/api/tls/client", json={"tab_id": tab_id}, headers=auth("admin"),
    )

    assert conflict.status_code == 400
    assert "another profile" in conflict.json()["error"]
    assert tls_clients.snapshot()[tab_id] == "alice"


def test_tls_client_registry_prunes_only_long_stale_entries(monkeypatch):
    tls_clients.clear()
    now = [100.0]
    monkeypatch.setattr(tls_clients, "STALE_AFTER_SECONDS", 30.0)
    monkeypatch.setattr(tls_clients.time, "monotonic", lambda: now[0])
    tls_clients.register("suspended-tab-0001", "alice")

    now[0] = 129.0
    assert tls_clients.snapshot() == {"suspended-tab-0001": "alice"}
    now[0] = 131.0
    assert tls_clients.snapshot() == {}


def test_tls_client_registry_is_bounded_per_profile_and_globally(monkeypatch):
    tls_clients.clear()
    monkeypatch.setattr(tls_clients, "MAX_CLIENTS_PER_PROFILE", 2)
    monkeypatch.setattr(tls_clients, "MAX_CLIENTS_TOTAL", 3)
    tls_clients.register("alice-browser-tab-1", "alice")
    tls_clients.register("alice-browser-tab-2", "alice")
    # Heartbeats update existing entries even when the profile is at its cap.
    tls_clients.register("alice-browser-tab-1", "alice")
    with pytest.raises(ValueError, match="profile has too many"):
        tls_clients.register("alice-browser-tab-3", "alice")
    tls_clients.register("admin-browser-tab-1", "admin")
    with pytest.raises(ValueError, match="server has too many"):
        tls_clients.register("bob-browser-tab-001", "bob")


def test_tls_client_and_durable_ticket_capacity_match():
    assert tls_clients.MAX_CLIENTS_PER_PROFILE == transition.MAX_ACTIVE_TICKETS_PER_PROFILE
    assert tls_clients.MAX_CLIENTS_TOTAL == transition.MAX_ACTIVE_TICKETS_TOTAL


def test_authenticated_client_heartbeat_registers_its_exact_http_alias(
    client, environment, monkeypatch,
):
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", real_ensure_local_tls)
    prepared(client)

    response = client.post(
        "http://cremind.lan:1515/api/tls/client",
        json={"tab_id": "alice-lan-tab-0001"}, headers=auth("alice"),
    )
    assert response.status_code == 200
    current = transition.load_transition()
    assert "http://cremind.lan:1515" in current["source_origins"]
    assert response.json()["transition"]["certificate_sha256"] == current["certificate_sha256"]


def test_transition_source_aliases_are_http_only_and_bounded(client, monkeypatch):
    value = prepared(client)
    data = ticket_body(value)
    data.update(source_origin="https://testserver:80", target_origin="https://testserver:80")
    assert client.post(
        "https://testserver:80/api/tls/handoff", json=data, headers=auth("alice"),
    ).status_code == 400

    monkeypatch.setattr(transition, "MAX_SOURCE_ORIGINS", 1)
    with pytest.raises(ValueError, match="too many source origins"):
        transition.register_source(transition.load_transition(), "http://another.test:1515")
    rejected = client.post(
        "http://another.test:1515/api/tls/client",
        json={"tab_id": "unprepared-alias-tab"}, headers=auth("alice"),
    )
    assert rejected.status_code == 400
    assert "unprepared-alias-tab" not in tls_clients.snapshot()


def test_corrupt_private_quiesce_ack_list_does_not_break_public_status(client):
    value = prepared(client)
    value.update(
        phase="quiescing",
        quiesce_expected={"alice-browser-tab-001": "alice"},
        quiesce_acked=[{"not": "a tab id"}],
    )
    transition.save_transition(value, announce=False)

    status = client.get("/api/tls/status")

    assert status.status_code == 200
    assert status.json()["quiesce_pending"] == 1


def test_quiescing_transition_can_be_cancelled(client):
    value = prepared(client)
    tab_id = "admin-browser-tab-001"
    assert client.post("/api/tls/client", json={"tab_id": tab_id}, headers=auth()).status_code == 200
    first = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert first.json()["transition"]["phase"] == "quiescing"
    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["transition"]["phase"] == "cancelled"
    private = transition.load_transition()
    assert "quiesce_expected" not in private and "quiesce_acked" not in private


def test_activate_can_leave_supervised_native_restart_to_operator(client, environment, monkeypatch):
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart",
        lambda: pytest.fail("restart=false must not schedule shutdown"),
    )
    value = prepared(client)
    result = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": False},
        headers=auth(),
    )
    assert result.status_code == 202
    assert result.json()["restart_required"] is True
    assert result.json()["restart_scheduled"] is False
    # Supervision exists but was declined, so nobody is coming to finish this:
    # the operator gets the runbook and the way out, not a spinner.
    assert result.json()["transition"]["awaiting_operator"] is True
    assert result.json()["can_cancel"] is True


def test_supervisor_schedule_failure_returns_committed_manual_recovery(
    client, monkeypatch,
):
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")

    def fail_restart():
        raise OSError("service manager unavailable")

    monkeypatch.setattr("app.api.system.schedule_system_restart", fail_restart)
    value = prepared(client)
    result = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": True},
        headers=auth(),
    )
    assert result.status_code == 202
    payload = result.json()
    assert payload["transition"]["phase"] == "activating"
    assert payload["restart_required"] is True
    assert payload["restart_scheduled"] is False
    # The recovery command is a step of its own, so the copy button on it yields
    # a runnable line instead of the sentence explaining why it is needed.
    assert payload["restart_error"].startswith("HTTPS was saved")
    assert "cremind server restart --yes" not in payload["restart_error"]
    assert payload["steps"][-1] == {"kind": "command", "text": "cremind server restart --yes"}
    assert payload["restart_error"] in payload["instructions"]
    assert payload["instructions"][-2:] == [
        payload["restart_error"], "cremind server restart --yes",
    ]
    # The restart that was supposed to finish this is not coming, so the switch
    # is immediately a waiting one: runnable command, and a way to back out.
    assert payload["transition"]["awaiting_operator"] is True
    assert payload["can_cancel"] is True
    assert transition.load_transition()["restart_planned"] is False


@pytest.mark.parametrize("install_mode", ["docker", "kubernetes"])
def test_after_setup_container_activation_schedules_supervised_restart(
        client, environment, monkeypatch, install_mode):
    monkeypatch.setenv("INSTALL_MODE", install_mode)
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "after-setup")
    scheduled = []
    monkeypatch.setattr("app.api.system.schedule_system_restart", lambda: scheduled.append(True) or 456)
    value = prepared(client)
    result = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": True},
        headers=auth(),
    )
    assert result.status_code == 202
    assert result.json()["management"] == "external"
    assert result.json()["restart_scheduled"] is True
    assert result.json()["restart_required"] is False
    assert scheduled == [True]


def test_token_files_are_reissued_when_https_starts_serving_not_at_activation(
    client, environment, monkeypatch,
):
    from app.auth.tokens import token_file_path, verify_token, write_token_file

    expires = int(time.time()) + 1800
    old = token("alice", expires=expires)
    write_token_file("alice", old)
    value = prepared(client)

    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    assert result.status_code == 202, result.text
    # Activation records where the boundary is going and stops there. Moving it
    # now would kill every session while the only reachable server is still the
    # plaintext one — the lockout this ordering exists to prevent.
    recorded = transition.load_transition()
    assert recorded["pending_transport_epoch"] == 1 and "transport_epoch" not in recorded
    assert token_file_path("alice").read_text(encoding="utf-8") == old
    assert verify_token(old) is not None

    served_https(monkeypatch)
    transition.mark_active()
    advanced = transition.load_transition()
    assert advanced["transport_epoch"] == 1 and "pending_transport_epoch" not in advanced
    assert advanced["phase"] == "active"

    migrated = token_file_path("alice").read_text(encoding="utf-8")
    claims = jwt.decode(migrated, SECRET, algorithms=["HS256"])
    old_claims = jwt.decode(old, SECRET, algorithms=["HS256"])
    assert claims["tep"] == 1
    assert claims["exp"] == expires
    assert claims["iat"] == old_claims["iat"] and claims["tsr"] == old_claims["tsr"]
    assert verify_token(old) is None
    assert verify_token(migrated) is not None


def test_transition_write_failure_restores_token_files_and_native_settings(
    client, environment, monkeypatch,
):
    from app.auth.tokens import token_file_path, write_token_file

    old = token("alice")
    write_token_file("alice", old)
    original_env = b"CREMIND_SSL=false\nAPP_URL=http://testserver\n"
    (environment / ".env").write_bytes(original_env)
    value = prepared(client)
    real_save = transition.save_transition

    def fail_activation(metadata, *, announce=True):
        if metadata.get("phase") == "activating":
            raise OSError("transition write failed")
        return real_save(metadata, announce=announce)

    monkeypatch.setattr(transition, "save_transition", fail_activation)
    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())

    assert result.status_code == 400
    assert token_file_path("alice").read_text(encoding="utf-8") == old
    assert (environment / ".env").read_bytes() == original_env
    assert transition.load_transition()["phase"] == "prepared"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://localhost:1515/api/oauth/callback", "https://localhost:1515/api/oauth/callback"),
        ("http://testserver/api/oauth/callback", "https://testserver:80/api/oauth/callback"),
        ("https://oauth.example/callback", "https://oauth.example/callback"),
    ],
)
def test_native_activation_moves_only_default_or_matching_atlassian_callbacks(
    client, environment, monkeypatch, configured, expected,
):
    (environment / ".env").write_text(
        f"APP_URL=http://testserver\nCREMIND_ATLASSIAN_REDIRECT_URI={configured}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREMIND_ATLASSIAN_REDIRECT_URI", configured)
    value = prepared(client)
    response = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": False}, headers=auth(),
    )
    assert response.status_code == 202
    content = (environment / ".env").read_text(encoding="utf-8")
    assert content.count("CREMIND_ATLASSIAN_REDIRECT_URI=") == 1
    assert f"CREMIND_ATLASSIAN_REDIRECT_URI={expected}" in content
    assert os.environ["CREMIND_ATLASSIAN_REDIRECT_URI"] == expected
    instructions = response.json()["instructions"]
    if configured == "https://oauth.example/callback":
        assert not any("Atlassian developer console" in item for item in instructions)
    else:
        assert any(expected in item and "Atlassian developer console" in item for item in instructions)


@pytest.mark.parametrize("mode", ["docker", "kubernetes"])
def test_external_activation_never_rewrites_deployment_environment(client, environment, monkeypatch, mode):
    monkeypatch.setenv("INSTALL_MODE", mode)
    value = prepared(client)
    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).json()
    assert result["management"] == "external" and result["instructions"]
    guidance = " ".join(result["instructions"])
    assert "Atlassian developer console" in guidance
    assert ("CREMIND_ATLASSIAN_REDIRECT_URI" if mode == "docker"
            else "cremind.atlassianRedirectUri") in guidance
    assert not (environment / ".env").exists()


@pytest.mark.parametrize(
    ("install_mode", "extra_env", "expected_commands"),
    [
        pytest.param("docker", {}, ["docker compose up -d --force-recreate cremind"], id="docker"),
        pytest.param(
            "kubernetes", {},
            [
                "helm list --all-namespaces",
                f"helm upgrade <release> {CHART_REFERENCE} --version {CHART_VERSION} "
                "--namespace <namespace> --reuse-values --set cremind.ssl=auto",
                "kubectl --namespace <namespace> rollout status deployment/<release> --timeout=5m",
                "kubectl --namespace <namespace> port-forward svc/<release> 1515:80",
            ],
            id="kubernetes-in-pod",
        ),
        pytest.param(
            "kubernetes", {"CREMIND_TLS_TERMINATION": "edge", "CREMIND_UI_PORT": "80"},
            [
                "helm list --all-namespaces",
                "kubectl --namespace <namespace> create secret tls cremind-tls "
                "--cert=<path-to-fullchain.pem> --key=<path-to-privkey.pem>",
                f"helm upgrade <release> {CHART_REFERENCE} --version {CHART_VERSION} "
                "--namespace <namespace> --reuse-values -f <your-values.yaml>",
                "kubectl --namespace <namespace> rollout status deployment/<release> --timeout=5m",
                "kubectl --namespace <namespace> get ingress <release>",
                "curl --fail https://testserver/api/tls/status",
            ],
            id="ingress",
        ),
        pytest.param("native", {}, ["cremind serve"], id="unsupervised-native"),
        pytest.param(
            "native", {"CREMIND_UI_PORT": "0"}, ["cremind serve"], id="reverse-proxy",
        ),
        pytest.param(
            "native", {"CREMIND_UI_PORT": "0", "CREMIND_SUPERVISED": "1"},
            ["cremind server restart --yes"], id="reverse-proxy-supervised",
        ),
    ],
)
def test_status_steps_keep_commands_bare_and_ordered(
    client, monkeypatch, install_mode, extra_env, expected_commands,
):
    """Only a command may carry a copy button, so only a command may be a
    command: the UI pastes ``text`` verbatim into someone's terminal."""
    monkeypatch.setenv("INSTALL_MODE", install_mode)
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)

    payload = client.get("/api/tls/status").json()

    steps = payload["steps"]
    assert steps, "a deployment that must act needs a runbook"
    assert payload["instructions"] == [step["text"] for step in steps]
    for step in steps:
        assert step["kind"] in ("note", "command")
        assert step["text"] and "`" not in step["text"]
        if step["kind"] == "command":
            assert "\n" not in step["text"]
            assert not step["text"].endswith(".")
            assert not step["text"].startswith("Run ")
    assert [s["text"] for s in steps if s["kind"] == "command"] == expected_commands
    # The closing note tells the operator where the server will answer.
    assert steps[-1]["kind"] == "note" and payload["https_url"] in steps[-1]["text"]


def test_supervised_native_status_has_nothing_to_run(client, monkeypatch):
    """It restarts itself, so a runbook would be busywork."""
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    payload = client.get("/api/tls/status").json()
    assert payload["steps"] == [] and payload["instructions"] == []


def test_unsupervised_native_note_follows_the_phase(client, monkeypatch):
    before = client.get("/api/tls/status").json()["steps"]
    assert before[0]["text"].startswith("After activation")

    value = prepared(client)
    result = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": False}, headers=auth(),
    ).json()

    assert result["steps"][0]["text"].startswith("HTTPS settings are saved")


def test_an_https_server_lists_only_certificate_repair_steps(
    client, environment, monkeypatch,
):
    """Re-running the enable runbook against a server already on HTTPS was the
    illogical part; a broken certificate needs a reload, nothing more."""
    monkeypatch.setenv("INSTALL_MODE", "docker")
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)

    healthy = client.get("https://testserver:80/api/tls/status").json()
    assert healthy["serving_https"] and healthy["steps"] == []

    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", str(environment / "missing.pem"))
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", str(environment / "missing.key"))
    broken = client.get("https://testserver:80/api/tls/status").json()

    assert broken["certificate_error"]
    assert [step["kind"] for step in broken["steps"]] == ["note", "command"]
    assert broken["steps"][0]["text"].startswith("Once the replacement certificate")
    assert broken["steps"][1]["text"] == "docker compose up -d --force-recreate cremind"
    assert "CREMIND_SSL=auto" not in " ".join(broken["instructions"])


def test_electron_is_managed_but_never_self_restarts(client, environment, monkeypatch):
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "42")
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "after-setup")
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart",
        lambda: pytest.fail("Electron main must remain the only backend owner"),
    )
    value = prepared(client)
    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).json()
    assert result["management"] == "electron" and result["restart_required"]
    assert (environment / ".env").exists()


def test_activation_requires_the_clients_matching_certificate_fingerprint(client, monkeypatch):
    monkeypatch.setattr("app.config.tls_auto.ca_fingerprint_sha256", lambda _path: "AA:BB:CC")
    monkeypatch.setattr("app.config.tls_auto.leaf_fingerprint_sha256", lambda _path: "11:22:33")
    value = prepared(client)
    assert value["ca_sha256"] == "AA:BB:CC"
    assert value["certificate_sha256"] == "11:22:33"
    payload = {"transition_id": value["id"]}
    assert client.post("/api/tls/activate", json=payload, headers=auth()).status_code == 400
    # Trust confirmation and activation pin different certificates: replacing
    # the server leaf under the same already-trusted CA must still be detected.
    payload["ca_sha256"] = "AA:BB:CC"
    assert client.post("/api/tls/activate", json=payload, headers=auth()).status_code == 400
    payload["certificate_sha256"] = "11:22:33"
    assert client.post("/api/tls/activate", json=payload, headers=auth()).status_code == 202


def test_activation_rejects_a_replaced_local_leaf_under_the_same_ca(
    client, environment, monkeypatch,
):
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", real_ensure_local_tls)
    value = prepared(client)
    original_ca = value["ca_sha256"]
    original_leaf = value["certificate_sha256"]
    assert original_ca and original_leaf and original_ca != original_leaf

    # Expanding the SAN set regenerates only the leaf; the trusted CA remains.
    real_ensure_local_tls(str(environment), ["testserver", "another.testserver"])
    current = transition.certificate_info()
    assert current["ca_sha256"] == original_ca
    assert current["certificate_sha256"] != original_leaf

    response = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "certificate_sha256": original_leaf},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "certificate changed" in response.json()["error"].lower()


def test_handoff_preserves_own_profile_claims_route_and_draft_after_restart(client, monkeypatch):
    value = prepared(client)
    old_http_token = token("alice")
    response = client.post(
        "/api/tls/handoff", json=ticket_body(value),
        headers={"Authorization": f"Bearer {old_http_token}"},
    )
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    assert "alice" not in ticket and token("alice") not in ticket
    assert client.post("/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 403
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).status_code == 202
    served_https(monkeypatch)
    # The state is re-read from disk: no process-local registry is needed.
    transition.mark_active()
    result = client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket})
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["profile"] == "alice" and result["route"] == ticket_body(value)["route"]
    assert result["state"] == ticket_body(value)["state"]
    migrated_claims = jwt.decode(result["token"], SECRET, algorithms=["HS256"])
    assert migrated_claims["exp"] <= int(time.time()) + 3600
    assert migrated_claims["tep"] == 1
    from app.auth.tokens import verify_token
    assert verify_token(old_http_token) is None
    assert verify_token(result["token"]) is not None
    assert client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 400
    public = client.get("/api/tls/status").json()
    assert "claims" not in json.dumps(public) and "token" not in json.dumps(public)


def test_expired_revoked_cancelled_and_wrong_target_tickets_are_rejected(client, monkeypatch):
    value = prepared(client)
    data = ticket_body(value)
    result = client.post("/api/tls/handoff", json=data, headers={"Authorization": f"Bearer {token('alice', serial=2)}"})
    assert result.status_code == 401
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    assert client.post("https://wrong.example/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 400
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    monkeypatch.setattr("app.auth.tokens.serial_matches", lambda claims: False)
    revoked = client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket})
    assert revoked.status_code == 401
    assert revoked.json()["profile"] == "alice" and revoked.json()["route"] == data["route"]
    assert "token" not in revoked.json()


def test_ticket_has_one_atomic_consumer(client):
    value = prepared(client)
    ticket = client.post("/api/tls/handoff", json=ticket_body(value), headers=auth("alice")).json()["ticket"]
    def consume():
        try:
            return transition.redeem_ticket(ticket, "https://testserver:80")["profile"]
        except ValueError:
            return "already consumed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == ["alice", "already consumed"]


def test_handoff_ticket_is_bound_to_the_installation(client, environment):
    value = prepared(client)
    ticket = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]
    (environment / "tls" / "instance-id").write_text("f" * 48, encoding="ascii")

    with pytest.raises(ValueError, match="no longer matches"):
        transition.redeem_ticket(ticket, "https://testserver:80")


def test_active_handoff_ticket_storage_is_bounded_without_deleting_live_tickets(
    client, monkeypatch,
):
    value = prepared(client)
    monkeypatch.setattr(transition, "MAX_ACTIVE_TICKETS_PER_PROFILE", 2)
    first = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]
    second = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]

    blocked = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    )
    assert blocked.status_code == 400
    assert "too many active" in blocked.json()["error"]
    # Admission failure preserves every already-issued durable handoff.
    assert transition.redeem_ticket(first, "https://testserver:80")["profile"] == "alice"
    assert transition.redeem_ticket(second, "https://testserver:80")["profile"] == "alice"


def test_handoff_drops_other_profiles_drafts_and_non_allowlisted_state(client):
    value = prepared(client)
    data = ticket_body(value)
    data["state"]["drafts"]["cremind:draft:admin:secret"] = "another profile's draft"
    data["state"]["drafts"]["cremind:draft:alice:malformed"] = {"invalid": "not a string"}
    data["state"]["preferences"] = {"theme": "dark", "agent_token_admin": "secret",
                                    "chat_mode_alice": "agent", "reasoning_enabled_alice": "true",
                                    "chat_mode_admin": "secret", "reasoning_enabled_admin": "true",
                                    "rightPanelViewMode": "list"}
    data["state"]["arbitrary"] = {"credential": "do not retain"}
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    state = transition.redeem_ticket(ticket, "https://testserver:80")["state"]
    assert state == {"mount": "/electron-renderer/", "drafts": {"cremind:draft:alice:conversation": "unsent"},
                     "preferences": {"theme": "dark", "chat_mode_alice": "agent", "reasoning_enabled_alice": "true",
                                     "rightPanelViewMode": "list"}}


def test_missed_event_tab_can_mint_over_https_only_from_exact_old_origin(client):
    value = prepared(client)
    headers = {**auth("alice"), "Origin": "http://testserver"}
    result = client.post("https://testserver:80/api/tls/handoff", json=ticket_body(value), headers=headers)
    assert result.status_code == 200
    assert result.headers["access-control-allow-origin"] == "http://testserver"
    headers["Origin"] = "http://evil.example"
    assert client.post("https://testserver:80/api/tls/handoff", json=ticket_body(value), headers=headers).status_code == 403


def test_plaintext_origin_cannot_read_redeemed_https_token(client):
    value = prepared(client)
    ticket = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]

    blocked = client.post(
        "https://testserver:80/api/tls/handoff/redeem",
        json={"ticket": ticket},
        headers={"Origin": "http://testserver"},
    )

    assert blocked.status_code == 403
    assert "access-control-allow-origin" not in blocked.headers
    redeemed = client.post(
        "https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket},
    )
    assert redeemed.status_code == 200
    assert redeemed.json()["profile"] == "alice"


def test_after_setup_exposes_transition_before_wizard_restart(client, monkeypatch):
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "after-setup")
    result = client.get("/api/tls/status").json()
    assert result["transition"]["phase"] == "prepared"
    assert result["transition"]["id"] and not result["serving_https"]


def test_corrupt_transition_cannot_reset_the_transport_epoch(environment, monkeypatch):
    from app.auth.tokens import current_transport_epoch

    path = environment / "tls" / "transition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'{"version":1,"phase":"active","transport_epoch":'
    path.write_bytes(damaged)
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)

    assert current_transport_epoch() is None
    with pytest.raises(OSError, match="credential boundary"):
        transition.mark_active()
    assert path.read_bytes() == damaged
    assert current_transport_epoch() is None


def test_plaintext_recovery_never_executes_api(client):
    plain = TestClient(recovery_app)
    result = plain.get("/electron-renderer/")
    assert result.status_code == 200
    assert "localStorage" not in result.text and "sessionStorage" not in result.text
    assert "Authorization" not in result.text and "/api/tls/handoff" not in result.text
    assert "never reads or transfers login credentials" in result.text
    assert "#/login/" in result.text and "redirect=" in result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["connection"] == "close"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'sha256-" in result.headers["content-security-policy"]
    script = result.text.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in result.headers["content-security-policy"]
    assert "location" not in result.headers
    assert "strict-transport-security" not in result.headers
    for method, path in [
        ("POST", "/"),
        ("GET", "/api/me"),
        ("POST", "/api/tls/handoff"),
        ("GET", "/.well-known/agent-card.json"),
    ]:
        denied = plain.request(method, path, headers={"Accept": "text/html"})
        assert denied.status_code == 426
        assert denied.headers["connection"] == "close"
        assert denied.headers["cache-control"] == "no-store"


def test_plaintext_recovery_offers_a_way_in_when_the_certificate_is_untrusted(client):
    """This page is reached only once HTTPS is genuinely up, so a device that
    cannot verify the certificate would otherwise read it as a dead end and be
    stranded away from its own data. Continuing past the browser warning is a
    real way in, and the page has to say so."""
    result = TestClient(recovery_app).get("/")

    assert result.status_code == 200
    assert "Locked out?" in result.text
    assert "accept the browser's certificate warning" in result.text
    # Trusting the CA is the durable fix, not a precondition for getting in.
    assert "you do not have to do it first" in result.text
    # The escape must not become a plaintext transport for application data.
    assert "deliberately cannot carry application data" in result.text
    assert TestClient(recovery_app).get("/api/me").status_code == 426
    # Static-only additions must leave the pinned script hash intact.
    script = result.text.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in result.headers["content-security-policy"]
    # An interstitial can only be clicked through when HSTS is absent.
    assert "strict-transport-security" not in result.headers


def test_plaintext_serves_the_app_until_a_listener_actually_serves_https(
    client, environment, monkeypatch,
):
    """Nothing is taken away while the switch is still waiting to be applied.

    The process keeps running and never binds TLS, which is exactly the interval
    a Docker or Kubernetes operator lives in between clicking Activate and
    running the deployment change. Cutting the plaintext application off here
    left them with no HTTPS to reach and no HTTP left either — locked away from
    their own data with no way to back it up.
    """
    value = prepared(client)
    response = client.post(
        "/api/tls/activate",
        json={"transition_id": value["id"], "restart": False},
        headers=auth(),
    )
    assert response.status_code == 202
    assert transition.load_transition()["phase"] == "activating"

    status = client.get("/api/tls/status")
    assert status.status_code == 200
    assert status.json()["serving_https"] is False
    assert status.json()["transition"]["awaiting_operator"] is True
    assert status.json()["can_cancel"] is True
    # 404 from this fixture's route table, never the recovery app's 426.
    assert client.get("/").status_code != 426
    assert client.post("/api/auth/login", json={}).status_code != 426
    # And the session that started the switch still authenticates.
    assert client.post(
        "/api/tls/client", json={"tab_id": "alice-browser-tab-001"},
        headers={"Authorization": f"Bearer {token('alice')}"},
    ).status_code == 200

    # The separate loopback API remains usable by the local CLI/operator.
    internal = f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/status"
    assert client.get(internal).status_code == 200

    # Once HTTPS genuinely serves, the boundary moves and plaintext closes: a
    # login here would otherwise mint a credential valid on the secure origin.
    served_https(monkeypatch)
    transition.mark_active()
    assert transition.load_transition()["phase"] == "active"
    assert client.get("/api/tls/status").status_code == 426
    assert client.post("/api/auth/login", json={}).status_code == 426
    assert client.get("/").status_code == 200
    assert client.get(internal).status_code == 200


def test_a_waiting_switch_can_be_called_off_and_gives_the_old_transport_back(
    client, environment, monkeypatch,
):
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    alice = token("alice")
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]},
                       headers=auth()).status_code == 202
    assert client.get("/api/tls/status").json()["can_cancel"] is True

    cancelled = client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert cancelled.status_code == 200
    assert cancelled.json()["transition"]["phase"] == "cancelled"
    assert cancelled.json()["can_cancel"] is False
    stored = transition.load_transition()
    assert "pending_transport_epoch" not in stored and "transport_epoch" not in stored
    # Nothing had been invalidated, so calling it off is a complete undo.
    from app.auth.tokens import verify_token
    assert verify_token(alice) is not None
    assert client.get("/api/tls/status").status_code == 200


@pytest.mark.parametrize("url", ["https://testserver:80", "internal"])
def test_cancel_is_refused_once_https_serves(client, environment, monkeypatch, url):
    value = prepared(client)
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    served_https(monkeypatch)
    transition.mark_active()

    base = f"http://127.0.0.1:{BaseConfig.PORT}" if url == "internal" else url
    refused = client.post(f"{base}/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert refused.status_code == 409
    assert "no longer be cancelled" in refused.json()["error"]
    assert transition.load_transition()["phase"] == "active"


def test_cancel_is_refused_after_an_external_deployment_change_landed(
    client, environment, monkeypatch,
):
    """The Ingress already serves HTTPS; only the epoch has not caught up.

    Cancelling here would leave the secure origin serving on the old boundary
    with every HTTP-era bearer still valid, so the switch has to go forward.
    """
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]},
                       headers=auth()).status_code == 202
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://testserver")

    refused = client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert refused.status_code == 409
    assert client.get("/api/tls/status").json()["can_cancel"] is False


def test_a_forged_https_scheme_cannot_move_the_credential_boundary(
    client, environment, monkeypatch,
):
    """uvicorn trusts ``X-Forwarded-Proto`` from loopback by default.

    On a deployment that does not delegate TLS, honouring that claim would let
    any local peer kill every session, re-sign the on-host token files and lock
    plaintext — recreating this very lockout on demand.
    """
    value = prepared(client)
    alice = token("alice")
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    storage_ready(monkeypatch)

    assert client.get("https://testserver:80/api/tls/status").status_code == 200
    stored = transition.load_transition()
    assert stored["phase"] == "activating" and stored["pending_transport_epoch"] == 1
    assert "transport_epoch" not in stored
    from app.auth.tokens import verify_token
    assert verify_token(alice) is not None
    assert client.get("/api/tls/status").status_code == 200


def test_the_boundary_waits_for_storage_before_retiring_any_session(
    client, environment, monkeypatch,
):
    value = prepared(client)
    alice = token("alice")
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    # HTTPS is up but the database is not: re-signing consults the profile
    # serials, and an empty snapshot would silently skip every rotated
    # profile's token file and strand it at the old epoch forever.
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    transition.mark_active()
    assert transition.load_transition()["phase"] == "activating"
    from app.auth.tokens import verify_token
    assert verify_token(alice) is not None

    storage_ready(monkeypatch)
    transition.mark_active()
    assert transition.load_transition()["phase"] == "active"
    assert verify_token(alice) is None


def test_the_boundary_waits_for_a_readable_serial_snapshot(client, environment, monkeypatch):
    value = prepared(client)
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    served_https(monkeypatch)

    def unavailable(**_kwargs):
        raise RuntimeError("database is not reachable")

    monkeypatch.setattr("app.auth.serial.all_serials", unavailable)
    transition.mark_active()
    stored = transition.load_transition()
    assert stored["phase"] == "activating" and stored["pending_transport_epoch"] == 1
    assert "could not be re-signed" in stored["activation_error"]

    monkeypatch.setattr("app.auth.serial.all_serials", lambda **_kwargs: {"admin": 3, "alice": 3})
    stored["activation_error_at"] = time.time() - transition.ACTIVATION_RETRY_SECONDS - 1
    transition.save_transition(stored)
    transition.mark_active()
    advanced = transition.load_transition()
    assert advanced["phase"] == "active" and advanced["transport_epoch"] == 1
    assert "activation_error" not in advanced


def test_a_failed_reissue_is_reported_throttled_and_retried(client, environment, monkeypatch):
    value = prepared(client)
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    served_https(monkeypatch)
    attempts = []

    def broken(epoch):
        attempts.append(epoch)
        raise OSError("tokens directory vanished")

    monkeypatch.setattr("app.auth.tokens.reissue_token_files_for_epoch", broken)
    transition.mark_active()
    status = client.get("/api/tls/status").json()
    assert status["activation_error"] and "could not be re-signed" in status["activation_error"]
    assert status["transition"]["activation_error"] == status["activation_error"]
    assert transition.load_transition()["phase"] == "activating"

    # Plaintext tabs poll every 1.5s and each attempt decodes every token file.
    transition.mark_active()
    transition.mark_active()
    assert attempts == [1]


def test_a_release_rollback_that_completed_the_switch_is_repaired(
    client, environment, monkeypatch,
):
    """An older release marks a switch active without moving the boundary.

    Left alone, every HTTP-era bearer would stay valid over HTTPS for good, so
    the advance also runs for an ``active`` transition that still has a pending
    epoch recorded.
    """
    value = prepared(client)
    alice = token("alice")
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    rolled_back = transition.load_transition()
    rolled_back["phase"] = "active"
    transition.save_transition(rolled_back)

    served_https(monkeypatch)
    transition.mark_active()
    repaired = transition.load_transition()
    assert repaired["transport_epoch"] == 1 and "pending_transport_epoch" not in repaired
    from app.auth.tokens import verify_token
    assert verify_token(alice) is None


def test_a_redeemed_handoff_never_hands_back_a_doomed_epoch(client, environment, monkeypatch):
    value = prepared(client)
    ticket = client.post("/api/tls/handoff", json=ticket_body(value),
                         headers={"Authorization": f"Bearer {token('alice')}"}).json()["ticket"]
    assert client.post("/api/tls/activate", json={"transition_id": value["id"], "restart": False},
                       headers=auth()).status_code == 202
    # No status call first: redemption must not depend on something else having
    # advanced the boundary, or it would return a token about to be retired.
    served_https(monkeypatch)
    result = client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket})
    assert result.status_code == 200, result.text
    assert jwt.decode(result.json()["token"], SECRET, algorithms=["HS256"])["tep"] == 1
    assert transition.load_transition()["phase"] == "active"


def test_a_pending_epoch_that_does_not_follow_the_current_one_is_refused(environment):
    path = environment / "tls" / "transition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "version": 1, "id": "A" * 32, "phase": "activating",
        "instance_id": transition.instance_id(), "source_origin": "http://testserver",
        "source_origins": ["http://testserver"], "target_origin": "https://testserver:80",
        "created_at": time.time(), "expires_at": None,
    }
    path.write_text(json.dumps({**base, "transport_epoch": 0, "pending_transport_epoch": 5}),
                    encoding="utf-8")
    with pytest.raises(OSError, match="credential boundary"):
        transition.load_transition()
    path.write_text(json.dumps({**base, "transport_epoch": 0, "pending_transport_epoch": 1}),
                    encoding="utf-8")
    assert transition.load_transition()["pending_transport_epoch"] == 1


def test_an_upgrade_mid_switch_keeps_the_lock_of_the_release_that_activated(
    client, environment, monkeypatch,
):
    """A file written by the previous release already moved the boundary.

    It carries ``transport_epoch`` and no pending field, so every session is
    already dead: plaintext must stay closed, or a login there would mint a
    token valid on the secure origin.
    """
    value = prepared(client)
    old_style = transition.load_transition()
    old_style["phase"] = "activating"
    old_style["transport_epoch"] = 1
    transition.save_transition(old_style)

    assert client.get("/api/tls/status").status_code == 426
    assert client.get("/").status_code == 200
    assert client.post("/api/tls/cancel", json={"transition_id": value["id"]},
                       headers=auth()).status_code == 426

    served_https(monkeypatch)
    from app.auth.tokens import token_file_path, write_token_file
    write_token_file("alice", token("alice"))
    before = token_file_path("alice").read_bytes()
    transition.mark_active()
    assert transition.load_transition()["phase"] == "active"
    assert transition.load_transition()["transport_epoch"] == 1
    # Nothing to re-sign: that release did it at activation time.
    assert token_file_path("alice").read_bytes() == before


def test_plaintext_recovery_rejects_an_invalid_redirect_origin(environment):
    response = TestClient(recovery_app).get("/", headers={"Host": "user@example.test"})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid public origin."}
    assert response.headers["connection"] == "close"


def test_export_write_failure_rolls_back_every_native_setting(client, environment, monkeypatch):
    from app.config import credentials_file

    original = b"CREMIND_SSL=false\nAPP_URL=http://testserver\n"
    (environment / ".env").write_bytes(original)
    value = prepared(client)
    original_created_at = value["created_at"]
    real_write = credentials_file.write_credentials_file
    def fail(**kwargs):
        raise OSError("export write failed")
    monkeypatch.setattr("app.config.credentials_file.write_credentials_file", fail)
    result = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    assert result.status_code == 400
    assert (environment / ".env").read_bytes() == original
    assert BaseConfig.APP_URL == "http://testserver"
    restored = transition.load_transition()
    assert restored["phase"] == "prepared"
    assert restored["created_at"] > original_created_at
    assert not any(key.startswith("quiesce_") for key in restored)

    monkeypatch.setattr("app.config.credentials_file.write_credentials_file", real_write)
    retried = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert retried.status_code == 202
    assert retried.json()["transition"]["phase"] == "activating"


def test_native_port_80_does_not_accidentally_move_to_443(client, environment, monkeypatch):
    monkeypatch.setenv("CREMIND_UI_PORT", "80")
    value = prepared(client)
    assert value["target_origin"] == "https://testserver:80"
    data = ticket_body(value)
    data["target_origin"] = value["target_origin"]
    result = client.post("/api/tls/handoff", json=data, headers=auth("alice"))
    assert result.status_code == 200
    redeemed = client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": result.json()["ticket"]})
    assert redeemed.status_code == 200
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).status_code == 202
    import tomllib
    exported = tomllib.loads((environment / "install" / "credentials.toml").read_text())
    assert exported["app"]["spa_url"] == "https://testserver:80"


def test_edge_terminated_https_uses_trusted_scope_and_not_stale_local_ca(client, monkeypatch):
    monkeypatch.setenv("CREMIND_UI_PORT", "0")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://testserver")
    monkeypatch.setattr("app.config.tls_auto.ca_fingerprint_sha256", lambda _path: "OLD:CA")
    result = client.get("https://testserver/api/tls/status").json()
    assert result["serving_https"] and result["management"] == "external"
    assert result["certificate_kind"] == "external" and result["ca_sha256"] is None
    assert result["transition"]["phase"] == "active"
    plain = client.get("/api/tls/status", headers={"X-Forwarded-Proto": "https"})
    assert plain.status_code == 426  # raw untrusted header has no authority


def test_custom_pair_uses_leaf_fingerprint_and_never_previous_local_ca(client, environment, monkeypatch):
    cert, key = real_ensure_local_tls(str(environment), ["testserver"])
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", cert)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", key)
    result = client.get("/api/tls/status").json()
    assert result["mode"] == "custom" and result["certificate_kind"] == "custom"
    assert result["ca_sha256"] is None and result["certificate_sha256"]
    assert client.get("/ca.pem").status_code == 404
    value = prepared(client)
    response = client.post("/api/tls/activate", headers=auth(), json={
        "transition_id": value["id"], "certificate_sha256": result["certificate_sha256"],
    })
    assert response.status_code == 202


@pytest.mark.parametrize("problem", ["expired", "future", "hostname"])
def test_custom_certificate_dates_and_hostname_are_validated(client, environment, monkeypatch, problem):
    import datetime
    from pathlib import Path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    cert_path, key_path = real_ensure_local_tls(str(environment), ["testserver"])
    original = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    ca_key = serialization.load_pem_private_key((environment / "tls" / "ca.key").read_bytes(), password=None)
    now = datetime.datetime.now(datetime.timezone.utc)
    begin = now + datetime.timedelta(days=1) if problem == "future" else now - datetime.timedelta(days=2)
    end = now - datetime.timedelta(days=1) if problem == "expired" else now + datetime.timedelta(days=2)
    certificate = (x509.CertificateBuilder().subject_name(original.subject).issuer_name(original.issuer)
                   .public_key(original.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(begin).not_valid_after(end)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("elsewhere.test" if problem == "hostname" else "testserver")]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    Path(cert_path).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", cert_path)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", key_path)
    result = client.get("/api/tls/status").json()
    assert result["certificate_error"] and not result["ready"]
    response = client.post("/api/tls/prepare", headers=auth(), json={})
    assert response.status_code == 400
    assert not (environment / ".env").exists()


def test_authenticated_alias_is_added_to_generated_sans_before_restart(client, environment, monkeypatch):
    from cryptography import x509
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", real_ensure_local_tls)
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    assert (environment / "tls" / "ca.pem").is_file()
    data = ticket_body(value)
    data.update(source_origin="http://cremind.lan:1515", target_origin="https://cremind.lan:1515")
    response = client.post("http://cremind.lan:1515/api/tls/handoff", headers=auth("alice"), json=data)
    assert response.status_code == 200
    cert = x509.load_pem_x509_certificate((environment / "tls" / "cert.pem").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "testserver" in names and "cremind.lan" in names
    assert not (environment / ".env").exists()


def test_only_ticket_referenced_own_uploads_survive_restart_then_return_to_idle_cleanup(client, environment):
    from app.utils.uploads_tmp import wipe_all_on_startup
    own_dir = environment / "alice" / "uploads_tmp" / "conversation"
    other_dir = environment / "admin" / "uploads_tmp" / "secret"
    own_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    kept = own_dir / "keep.txt"
    kept.write_text("unsent attachment")
    unrelated = own_dir / "remove.txt"
    unrelated.write_text("not referenced")
    secret = other_dir / "secret.txt"
    secret.write_text("other profile")
    value = prepared(client)
    data = ticket_body(value)
    data["state"]["drafts"]["cremind:draft:alice:conversation"] = json.dumps({
        "text": "unsent", "attachments": [{"path": str(kept)}, {"path": str(secret)}],
    })
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    retained = transition.retained_upload_paths()
    assert retained == {str(kept.resolve())}
    wipe_all_on_startup(preserve=retained)
    assert kept.exists() and not unrelated.exists() and not secret.exists()
    transition.redeem_ticket(ticket, "https://testserver:80")
    assert not transition.retained_upload_paths()
    assert kept.exists()  # the restored composer still needs it; ordinary idle pruning resumes


def test_cancelled_and_expired_tickets_never_restore_sessions(client, monkeypatch):
    value = prepared(client)
    data = ticket_body(value)
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    assert client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 400
    value = prepared(client)
    ticket = client.post("/api/tls/handoff", json=ticket_body(value), headers=auth("alice")).json()["ticket"]
    now = time.time()
    monkeypatch.setattr(transition.time, "time", lambda: now + transition.TICKET_TTL + 1)
    transition.cleanup_tickets()
    for path in (transition.directory() / "handoffs").glob("*.json"):
        receipt = json.loads(path.read_text())
        assert "claims" not in receipt and "state" not in receipt
    expired = client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket})
    assert expired.status_code == 401 and expired.json()["route"] == data["route"]
    assert client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 400


def test_expired_route_receipts_are_removed_after_one_day(client, monkeypatch):
    value = prepared(client)
    client.post("/api/tls/handoff", json=ticket_body(value), headers=auth("alice"))
    now = time.time()
    monkeypatch.setattr(transition.time, "time", lambda: now + transition.TICKET_TTL + 1)
    transition.cleanup_tickets()
    assert list((transition.directory() / "handoffs").glob("*.json"))
    monkeypatch.setattr(transition.time, "time", lambda: now + transition.TICKET_TTL + transition.EXPIRED_ROUTE_TTL + 1)
    transition.cleanup_tickets()
    assert not list((transition.directory() / "handoffs").glob("*.json"))


def test_expired_route_receipts_are_bounded_but_keep_recent_login_routes(
    client, monkeypatch,
):
    value = prepared(client)
    monkeypatch.setattr(transition, "MAX_EXPIRED_ROUTE_RECEIPTS", 1)
    tickets = [
        client.post(
            "/api/tls/handoff",
            json={**ticket_body(value), "route": f"/alice/c/{index}"},
            headers=auth("alice"),
        ).json()["ticket"]
        for index in range(2)
    ]
    now = time.time()
    monkeypatch.setattr(transition.time, "time", lambda: now + transition.TICKET_TTL + 1)
    transition.cleanup_tickets()

    responses = [
        client.post("https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket})
        for ticket in tickets
    ]
    assert sorted(response.status_code for response in responses) == [400, 401]
    restored = next(response for response in responses if response.status_code == 401)
    assert restored.json()["route"].startswith("/alice/c/")
    assert "token" not in restored.json()


def test_admin_cli_can_prepare_a_public_hostname_from_the_internal_listener(client):
    result = client.post(f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/prepare", headers=auth(),
                         json={"source_origin": "http://public.local:1515"})
    assert result.status_code == 200
    assert result.json()["transition"]["target_origin"] == "https://public.local:1515"


@pytest.mark.parametrize("phase", ["prepared", "cancelled"])
def test_public_https_status_never_activates_or_resurrects_an_unapproved_transition(client, monkeypatch, phase):
    value = prepared(client)
    if phase == "cancelled":
        client.post("/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth())
    before = transition.load_transition()
    assert client.get("https://testserver:80/api/tls/status").status_code == 200
    assert transition.load_transition() == before
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    assert client.get("https://testserver:80/api/tls/status").status_code == 200
    assert transition.load_transition() == before


def test_active_transition_refreshes_certificate_and_port_facts_without_replacing_identity(client, monkeypatch):
    value = prepared(client)
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).status_code == 202
    served_https(monkeypatch)
    transition.mark_active()
    first = transition.load_transition()
    deadline = first["upload_recovery_until"]

    monkeypatch.setattr(transition, "certificate_info", lambda **_kwargs: {
        "certificate_kind": "custom", "certificate_sha256": "AA:BB", "ca_sha256": None,
    })
    monkeypatch.setattr(transition, "port_facts", lambda **_kwargs: {
        "same_public_port": False, "public_port": 443,
    })
    transition.mark_active()
    refreshed = transition.load_transition()

    assert refreshed["id"] == first["id"]
    assert refreshed["source_origin"] == first["source_origin"]
    assert refreshed["upload_recovery_until"] == deadline
    assert refreshed["certificate_kind"] == "custom"
    assert refreshed["certificate_sha256"] == "AA:BB"
    assert refreshed["same_public_port"] is False
    assert refreshed["public_port"] == 443


def test_suspended_tab_uploads_survive_restart_only_within_persisted_recovery_deadline(client, environment, monkeypatch):
    from app.utils.uploads_tmp import wipe_all_on_startup, prune_idle
    own = environment / "alice" / "uploads_tmp" / "conversation" / "unsent.txt"
    other = environment / "admin" / "uploads_tmp" / "secret" / "private.txt"
    for path in (own, other):
        path.parent.mkdir(parents=True)
        path.write_text("an unsent upload from a suspended tab")
    value = prepared(client)
    assert not transition.startup_upload_recovery_window()
    assert client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth()).status_code == 202
    deadline = transition.load_transition()["upload_recovery_until"]
    assert transition.startup_upload_recovery_window()
    assert not transition.retained_upload_paths()  # neither tab could announce itself
    wipe_all_on_startup()
    assert own.exists() and other.exists()
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    transition.mark_active()
    assert transition.load_transition()["upload_recovery_until"] == deadline
    wipe_all_on_startup()
    assert own.exists() and other.exists()
    data = ticket_body(value)
    data["state"]["drafts"] = {"cremind:draft:alice:conversation": json.dumps({"attachments": [{"path": str(own)}]}),
                               "cremind:draft:admin:secret": json.dumps({"attachments": [{"path": str(other)}]})}
    ticket = client.post("https://testserver:80/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    state = transition.redeem_ticket(ticket, "https://testserver:80")["state"]
    assert list(state["drafts"]) == ["cremind:draft:alice:conversation"]
    # Recovery changes neither profile authorization nor ordinary idle expiry.
    assert prune_idle(threshold_seconds=-1) == 2
    for path in (own, other):
        path.parent.mkdir(parents=True)
        path.write_text("another pending upload")
    monkeypatch.setattr(transition.time, "time", lambda: deadline + 1)
    assert not transition.startup_upload_recovery_window()
    wipe_all_on_startup()
    assert not own.exists() and not other.exists()


def test_http_edge_prepare_uses_external_certificate_and_recovers_after_activation(client, environment, monkeypatch):
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    monkeypatch.setenv("CREMIND_UI_PORT", "80")
    def no_ca(*_args):
        pytest.fail("Ingress preparation must never create a Cremind CA")
    monkeypatch.setattr("app.config.tls_auto.ensure_local_tls", no_ca)
    status = client.get("/api/tls/status").json()
    assert status["certificate_kind"] == "external" and not status["same_public_port"]
    assert status["ca_sha256"] is None and status["transition"] is None
    value = prepared(client)
    assert value["certificate_kind"] == "external" and value["ca_sha256"] is None
    assert value["target_origin"] == "https://testserver" and not value["same_public_port"]
    data = ticket_body(value)
    data["target_origin"] = value["target_origin"]
    ticket = client.post("/api/tls/handoff", json=data, headers=auth("alice")).json()["ticket"]
    response = client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    assert response.status_code == 202 and "ingress.tls" in " ".join(response.json()["instructions"])
    assert "Atlassian developer console" in " ".join(response.json()["instructions"])
    assert not (environment / "tls" / "ca.pem").exists() and not (environment / ".env").exists()
    # The Ingress has not been updated yet, so this pod is still the only way
    # in: it keeps serving, and a raw header cannot move the boundary to change
    # that. The Helm upgrade may be hours away, or may never come.
    storage_ready(monkeypatch)
    waiting = client.get("/api/tls/status", headers={"X-Forwarded-Proto": "https"})
    assert waiting.status_code == 200
    assert waiting.json()["transition"]["awaiting_operator"] is True
    assert client.post("/api/tls/handoff", json=data, headers=auth("alice")).status_code == 200

    # The rollout lands: the Ingress now terminates HTTPS and says so through
    # the trusted proxy layer, which is what closes the plaintext surface.
    status = client.get("https://testserver/api/tls/status").json()
    assert status["serving_https"] and status["ready"] and status["transition"]["phase"] == "active"
    assert client.get("/api/tls/status", headers={"X-Forwarded-Proto": "https"}).status_code == 426
    assert client.post("/api/tls/handoff", json=data, headers=auth("alice")).status_code == 426
    page = client.get("/")
    assert page.status_code == 200 and "new URL(\"https://testserver\")" in page.text
    assert f'expectedTransition="{value["id"]}"' in page.text
    assert "status.ready===false" in page.text and "status.transition.phase!=='active'" in page.text
    assert "href='/ca.pem'" not in page.text
    assert "new URL(\"https://testserver:80\")" not in page.text
    status = client.get("https://testserver/api/tls/status").json()
    assert status["serving_https"] and status["ready"] and status["transition"]["phase"] == "active"
    assert client.post("https://testserver/api/tls/handoff/redeem", json={"ticket": ticket}).status_code == 200
    assert client.get(f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/status").status_code == 200
    assert client.get(f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/status", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 426


def test_fresh_edge_https_install_restricts_plaintext_before_any_transition(client, monkeypatch):
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://testserver")
    assert transition.load_transition() is None
    assert client.get("/api/tls/status").status_code == 426
    assert client.get("/").status_code == 200
    assert client.get("https://testserver/api/tls/status").json()["serving_https"]


def test_custom_certificate_recovery_has_no_generated_ca_advice(client, environment, monkeypatch):
    cert, key = real_ensure_local_tls(str(environment), ["testserver"])
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", cert)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", key)
    page = TestClient(recovery_app).get("/")
    assert "href='/ca.pem'" not in page.text
    assert "certificate issuer" in page.text


def test_same_listener_ports_and_external_alias_ports_are_localized(monkeypatch, environment):
    assert transition.https_target("https://forward.local") == "https://forward.local"
    assert transition.https_target("http://forward.local") == "https://forward.local:80"
    assert transition.https_target("http://forward.local:443") == "https://forward.local"
    assert transition.http_source("https://forward.local") == "http://forward.local:443"
    monkeypatch.setenv("CREMIND_TLS_TERMINATION", "edge")
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://primary.local:8443")
    assert transition.https_target("http://alias.local") == "https://alias.local:8443"
