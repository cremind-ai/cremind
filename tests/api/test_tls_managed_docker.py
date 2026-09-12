"""A Compose install switches itself to HTTPS, with no runbook and no recreate.

The runbook this replaces asked the operator to edit four values in the `.env`
beside their `docker-compose.yml` and recreate the container. Every one of them
is something Cremind already computes exactly, and two could not be delivered
that way at all — `CREMIND_ATLASSIAN_REDIRECT_URI` is absent from the shipped
Compose template, and the rendered `docker-compose.yml` on the host is frozen at
install time.

Fixtures come from ``test_tls_transition`` so both files describe the same
installation; see the module docstring there.
"""
import pytest

from app.config import tls_managed_env as managed, tls_transition as transition
from app.config.settings import BaseConfig

from tests.api.test_tls_transition import (  # noqa: F401 - fixtures by name
    auth,
    client,
    environment,
    prepared,
    served_https,
    storage_ready,
    token,
)


@pytest.fixture
def compose(environment, monkeypatch):  # noqa: F811
    """A Compose install, as the shipped bundle produces one."""
    monkeypatch.setenv("INSTALL_MODE", "docker")
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(environment))
    return environment


def overlay(volume):
    return volume / "tls" / "managed-env"


# ── which deployments Cremind may switch by itself ────────────────────────


def test_a_compose_install_is_cremind_s_own_to_switch(compose):
    assert transition.management() == "managed-docker"
    assert transition.canonical_env_path() == overlay(compose)


@pytest.mark.parametrize(
    ("mode", "extra", "expected"),
    [
        ("kubernetes", {}, "external"),
        ("native", {}, "native"),
        # No public bind of its own, or an explicitly configured terminator:
        # the public origin is not this process's to change either way.
        ("docker", {"CREMIND_UI_PORT": "0"}, "external"),
        ("docker", {"CREMIND_TLS_TERMINATION": "edge"}, "external"),
    ],
)
def test_every_other_deployment_keeps_its_manager(
    compose, monkeypatch, mode, extra, expected,
):
    monkeypatch.setenv("INSTALL_MODE", mode)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)

    assert transition.management() == expected


def test_kubernetes_is_excluded_on_purpose(compose, monkeypatch):
    """Enabling TLS on the chart moves the Service, the probes and the proxy
    sidecar together — a chart change no pod can make to itself."""
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")

    assert transition.management() == "external"
    assert transition.canonical_env_path() == compose / ".env"


# ── activation ────────────────────────────────────────────────────────────


def test_activation_persists_into_the_volume(compose, client, monkeypatch):  # noqa: F811
    """Into the system-directory volume, which survives both a restart and a
    container replacement — not into the deployment, which Cremind cannot
    reach, and not into the host's frozen docker-compose.yml."""
    value = prepared(client)
    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )

    assert result.status_code == 202
    assert result.json()["management"] == "managed-docker"
    written = overlay(compose).read_text(encoding="utf-8")
    assert "CREMIND_SSL=true" in written
    assert "APP_URL=https://testserver:80" in written
    assert "http://testserver,https://testserver:80" in written
    # The key the Compose template has no slot for at all.
    assert "CREMIND_ATLASSIAN_REDIRECT_URI=https://testserver:80/api/oauth/callback" in written
    # The deployment's own files are untouched.
    assert not (compose / ".env").exists()


def test_the_switch_survives_the_restart_it_schedules(compose, client, monkeypatch):  # noqa: F811
    """The mechanism in one test: persist, then let a fresh process read it.

    A restart re-runs the entrypoint with the container's creation-time
    environment, so without this file the settings would simply be gone.
    """
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).status_code == 202

    # A new process: nothing of the switch is in the environment it inherits.
    for key in managed.MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    applied = managed.load_into_environ()

    assert applied["CREMIND_SSL"] == "true"
    assert applied["APP_URL"] == "https://testserver:80"
    assert applied["CREMIND_ATLASSIAN_REDIRECT_URI"].startswith("https://testserver:80")


def test_activation_restarts_the_container_instead_of_printing_commands(
    compose, client, monkeypatch,  # noqa: F811
):
    scheduled = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: scheduled.append(True) or 99,
    )
    value = prepared(client)

    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).json()

    assert scheduled == [True]
    assert result["restart_scheduled"] is True
    assert result["restart_required"] is False
    # Notes explaining what happens, and nothing to paste into a terminal.
    assert result["steps"], "the operator still deserves to know what is going on"
    assert {step["kind"] for step in result["steps"]} == {"note"}
    assert "docker compose" not in " ".join(result["instructions"])
    assert "Cremind applies this switch itself" in result["steps"][0]["text"]
    # The one thing that still needs a human, because it is in someone else's
    # console: the Atlassian callback the switch just moved.
    assert any("Atlassian developer console" in step["text"] for step in result["steps"])


def test_a_managed_switch_is_one_cremind_can_undo(compose, client, monkeypatch):  # noqa: F811
    """It persisted the change and scheduled the restart, so it owns both
    halves of the undo — and therefore waits for a confirming client and
    carries a deadline, exactly like a supervised native switch."""
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())

    stored = transition.load_transition()
    assert stored["self_applied"] is True
    assert stored["confirmation_deadline"] > 0
    assert transition.requires_confirmation(stored) is True
    assert (compose / "tls" / "native-rollback.json").exists()


def test_cancelling_restores_the_volume_and_leaves_no_trace(
    compose, client, monkeypatch,  # noqa: F811
):
    import time

    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    assert overlay(compose).exists()
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["revert_error"] is None
    # The file did not exist before the switch, so restoring means removing it.
    assert not overlay(compose).exists()
    assert BaseConfig.APP_URL == "http://testserver"


def test_an_unreachable_managed_switch_reverts_itself(
    compose, client, monkeypatch,  # noqa: F811
):
    """End to end: the failure the whole design exists for. HTTPS comes up
    perfectly and no browser can open it, so the container puts itself back."""
    import asyncio
    import time

    from app.server import _watch_https_confirmation

    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    served_https(monkeypatch)
    overdue = transition.load_transition()
    overdue["confirmation_deadline"] = time.time() - 1
    transition.save_transition(overdue)
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 99,
    )

    asyncio.run(_watch_https_confirmation())

    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert "no browser reached it" in stored["auto_reverted"]["reason"]
    assert not overlay(compose).exists()
    assert restarts == [True]
