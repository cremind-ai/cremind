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
import json

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


@pytest.fixture
def unlabelled(environment, monkeypatch):  # noqa: F811
    """A container started without ``INSTALL_MODE`` — ``docker run`` on the
    image, a hand-written compose file, a ``.env`` that predates the key — where
    only the container marker says what it is."""
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(environment))
    marker = environment / "dockerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", marker)
    return environment


def overlay(volume):
    return volume / "tls" / "managed-env"


def rollback_record(volume):
    return volume / "tls" / "native-rollback.json"


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


def test_a_container_that_never_said_install_mode_is_cremind_s_own_to_switch(unlabelled):
    """The shipped compose template sets INSTALL_MODE=docker; the image does
    not. Reading the variable raw made this container a native install here
    while the Developer page, reading the marker, called it Docker."""
    assert transition.management() == "managed-docker"
    assert transition.canonical_env_path() == overlay(unlabelled)


def test_a_container_that_never_said_install_mode_switches_itself(
    unlabelled, client, monkeypatch,  # noqa: F811
):
    """The report this fixes, end to end.

    Settings → HTTPS & Certificate on a local Docker install showed the runbook
    for an *unsupervised native* server ("nothing supervises this server …
    Ctrl+C … cremind serve"), persisted the switch into ``/root/.cremind/.env``
    — which the container environment shadows — and scheduled no restart; a
    restart from the Developer page then changed nothing, because nothing that
    boot read had changed. It is a Compose install and gets the Compose switch.
    """
    scheduled = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: scheduled.append(True) or 99,
    )
    value = prepared(client)
    status = client.get("/api/tls/status", headers=auth()).json()
    assert status["management"] == "managed-docker"
    assert status["install_mode"] == "docker"
    assert status["restart_supported"] is True

    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )

    assert result.status_code == 202, result.text
    body = result.json()
    assert body["restart_scheduled"] is True and scheduled == [True]
    assert "CREMIND_SSL=true" in overlay(unlabelled).read_text(encoding="utf-8")
    assert not (unlabelled / ".env").exists()
    assert {step["kind"] for step in body["steps"]} == {"note"}
    assert not any("Ctrl+C" in step["text"] for step in body["steps"])
    assert "Cremind applies this switch itself" in body["steps"][0]["text"]
    stored = transition.load_transition()
    assert stored["self_applied"] is True
    assert stored["confirmation_deadline"] > 0


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


def activated_as_native_inside_the_container(unlabelled, client, monkeypatch):
    """Activate the way a release that read INSTALL_MODE raw did it.

    The marker is hidden for the duration, so the container reads as an
    unsupervised native install: the switch goes into ``.env``, no restart is
    scheduled, ``self_applied`` is never set. Returns the marker, so the caller
    can put it back and boot under the current reading.
    """
    marker = managed._CONTAINER_MARKER
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", unlabelled / "no-dockerenv")
    (unlabelled / ".env").write_text("KEEP_ME=one\nCREMIND_SSL=false\n", encoding="utf-8")
    value = prepared(client)
    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert result.status_code == 202, result.text
    body = result.json()
    assert body["management"] == "native"
    assert body["restart_supported"] is False
    # The runbook the report quoted, word for word.
    assert any("nothing supervises this server" in step["text"] for step in body["steps"])
    assert "CREMIND_SSL=true" in (unlabelled / ".env").read_text(encoding="utf-8")
    assert not overlay(unlabelled).exists()
    stored = transition.load_transition()
    assert stored["phase"] == "activating"
    assert stored.get("self_applied") is not True
    return marker


# ── a switch a mistaken release left behind ───────────────────────────────


def test_a_switch_saved_as_native_inside_a_container_is_called_off_at_boot(
    unlabelled, client, monkeypatch,  # noqa: F811
):
    """What the reported install is left holding once it upgrades.

    The switch sits in ``activating`` with no restart planned and no
    ``self_applied``, so the boot counter ignores it, and under the corrected
    reading the page would show a spinner promising a container restart that
    is never coming. The first boot that can read the container correctly
    calls it off, puts ``.env`` back and says why, so the administrator can
    simply switch again.
    """
    marker = activated_as_native_inside_the_container(unlabelled, client, monkeypatch)
    # A restart under the mistaken release (the Developer page in the report)
    # changed nothing, and its boot did not notice: the switch is not one it
    # counts.
    assert transition.revert_stranded_managed_switch(serving_https=False) is None
    assert transition.reconcile_activation_boot(serving_https=False) is None
    assert transition.load_transition()["phase"] == "activating"

    # The next boot, reading the container for what it is.
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", marker)
    reason = transition.revert_stranded_managed_switch(serving_https=False)

    assert reason is not None and "as if the server were a native install" in reason
    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert stored["auto_reverted"]["reason"] == reason
    assert stored["auto_reverted"]["restored"] is True
    assert (unlabelled / ".env").read_text(encoding="utf-8") == "KEEP_ME=one\nCREMIND_SSL=false\n"
    assert not rollback_record(unlabelled).exists()
    # Once is enough.
    assert transition.revert_stranded_managed_switch(serving_https=False) is None


def test_a_switch_prepared_as_external_by_an_older_release_is_left_for_its_operator(
    compose, monkeypatch,
):
    """Before the managed path existed, a labelled Compose install was
    ``external``: activation persisted nothing, wrote no rollback record and
    handed the operator the recreate runbook. Upgraded mid-runbook, that switch
    is activating with no overlay — and must wait for the operator it was
    promised to, not be called off with "settings could not be restored" about
    settings nobody changed. The transition says what it was prepared as."""
    import secrets
    import time

    transition.save_transition({
        "version": 1, "id": secrets.token_urlsafe(24), "phase": "activating",
        "instance_id": transition.instance_id(),
        "source_origin": "http://testserver", "source_origins": ["http://testserver"],
        "target_origin": "https://testserver:80", "created_at": time.time(),
        "expires_at": None, "management": "external",
        "pending_transport_epoch": 1, "restart_planned": False,
        "activated_at": time.time() - 3600,
    })
    assert transition.management() == "managed-docker"
    assert not overlay(compose).exists()

    for _ in range(3):
        assert transition.revert_stranded_managed_switch(serving_https=False) is None

    assert transition.load_transition()["phase"] == "activating"


def test_a_managed_switch_whose_overlay_vanished_is_called_off_with_the_other_reason(
    compose, client, monkeypatch,  # noqa: F811
):
    """Same dead end, different cause: the switch was managed all along and
    the file it depends on has gone (a volume no longer mounted, a hand
    deletion). The record shows where it was persisted, so the administrator
    is told about the volume rather than about a native install."""
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"], "restart": False},
        headers=auth(),
    ).status_code == 202
    overlay(compose).unlink()

    reason = transition.revert_stranded_managed_switch(serving_https=False)

    assert reason == transition.STRANDED_OVERLAY_MISSING_REASON
    assert "volume" in reason and "native install" not in reason
    assert transition.load_transition()["phase"] == "cancelled"


def test_a_managed_switch_with_its_overlay_in_place_is_left_alone(
    compose, client, monkeypatch,  # noqa: F811
):
    """A real managed switch waiting for its restart — including ``--no-restart``,
    where the operator owns the timing — is exactly what must not be undone."""
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"], "restart": False},
        headers=auth(),
    ).status_code == 202
    assert overlay(compose).exists()

    for _ in range(3):
        assert transition.revert_stranded_managed_switch(serving_https=False) is None
    assert transition.load_transition()["phase"] == "activating"


def test_a_boot_that_serves_https_never_calls_a_switch_off(
    compose, client, monkeypatch,  # noqa: F811
):
    """The overlay retires itself once the deployment carries the values, and
    that boot serves HTTPS: no overlay, but nothing is stranded either."""
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    overlay(compose).unlink()

    assert transition.revert_stranded_managed_switch(serving_https=True) is None
    assert transition.load_transition()["phase"] == "activating"


# ── where the undo puts the file back ─────────────────────────────────────


def test_the_rollback_record_names_the_file_it_came_from(
    compose, client, monkeypatch,  # noqa: F811
):
    """``canonical_env_path`` can change between the write and the undo — this
    very fix moves it for a container that never said INSTALL_MODE — and the
    record has to survive that, or the old ``.env`` bytes land in the overlay
    and become next boot's HTTPS settings."""
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())

    record = json.loads(rollback_record(compose).read_text(encoding="utf-8"))

    assert record["env_path"] == str(overlay(compose))


def test_a_record_from_before_env_path_is_restored_where_that_release_wrote_it(
    unlabelled, client, monkeypatch,  # noqa: F811
):
    """Records from 0.0.17rc16.dev1–3 carry no path. That release chose by the
    raw variable alone, which a container's environment fixes — so the choice
    is reconstructible: the overlay when it says ``docker``, ``.env`` otherwise."""
    marker = activated_as_native_inside_the_container(unlabelled, client, monkeypatch)
    record = json.loads(rollback_record(unlabelled).read_text(encoding="utf-8"))
    del record["env_path"]
    rollback_record(unlabelled).write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", marker)

    assert transition.revert_stranded_managed_switch(serving_https=False) is not None

    assert (unlabelled / ".env").read_text(encoding="utf-8") == "KEEP_ME=one\nCREMIND_SSL=false\n"
    assert not overlay(unlabelled).exists()


def test_a_record_from_before_env_path_survives_the_operator_labelling_the_container(
    unlabelled, client, monkeypatch,  # noqa: F811
):
    """The boot log asks the operator to set INSTALL_MODE=docker, and an image
    upgrade on Compose is the recreate at which they would do it. Deciding an
    old record by that variable would then steer the pre-switch ``.env`` bytes
    into the overlay — an override nothing could ever retire — and call the
    result "the volume is no longer there". The overlay's presence on disk is
    what says where the bytes came from."""
    marker = activated_as_native_inside_the_container(unlabelled, client, monkeypatch)
    record = json.loads(rollback_record(unlabelled).read_text(encoding="utf-8"))
    del record["env_path"]
    rollback_record(unlabelled).write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", marker)
    monkeypatch.setenv("INSTALL_MODE", "docker")

    reason = transition.revert_stranded_managed_switch(serving_https=False)

    assert reason == transition.STRANDED_MANAGED_SWITCH_REASON
    assert (unlabelled / ".env").read_text(encoding="utf-8") == "KEEP_ME=one\nCREMIND_SSL=false\n"
    assert not overlay(unlabelled).exists()
    assert managed.load_into_environ() == {}


def test_a_container_that_never_said_install_mode_cannot_trust_for_the_browser(unlabelled):
    """The CA-trust guidance reads the same resolver: a container can only
    write its own trust store, whatever its environment says about itself."""
    from app.api.tls import _trust_environment_error

    assert "container" in (_trust_environment_error() or "")


def test_a_record_from_before_env_path_on_a_labelled_compose_install_restores_the_overlay(
    compose, client, monkeypatch,  # noqa: F811
):
    import time

    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    record = json.loads(rollback_record(compose).read_text(encoding="utf-8"))
    del record["env_path"]
    rollback_record(compose).write_text(json.dumps(record), encoding="utf-8")
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert cancelled.status_code == 200 and cancelled.json()["revert_error"] is None
    assert not overlay(compose).exists()
    assert not (compose / ".env").exists()


def test_the_record_follows_a_relocated_system_directory(
    compose, client, monkeypatch, tmp_path,  # noqa: F811
):
    """``cremind relocate`` between the switch and its undo moves every file
    the record refers to. The record names a *kind* of file, resolved against
    wherever the system directory is now."""
    import shutil
    import time

    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    moved = tmp_path / "relocated"
    shutil.copytree(compose / "tls", moved / "tls")
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(moved))
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(moved))
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    assert overlay(moved).exists()

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert cancelled.status_code == 200 and cancelled.json()["revert_error"] is None
    assert not overlay(moved).exists()


@pytest.mark.parametrize("elsewhere", ["somewhere-else", "tls/.env.bak", "   "])
def test_a_record_naming_any_other_file_is_refused(
    compose, client, monkeypatch, elsewhere,  # noqa: F811
):
    """Only the two files the switch has ever written. A tampered record must
    not turn a cancel into a write somewhere else."""
    import time

    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    record = json.loads(rollback_record(compose).read_text(encoding="utf-8"))
    target = compose / elsewhere.strip() if elsewhere.strip() else None
    record["env_path"] = str(target) if target else elsewhere
    rollback_record(compose).write_text(json.dumps(record), encoding="utf-8")
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert "never wrote" in (cancelled.json().get("revert_error") or cancelled.text)
    if target is not None:
        assert not target.exists()
    assert overlay(compose).exists()


def test_a_stale_installer_app_url_is_repaired_into_the_volume(
    compose, client, monkeypatch,  # noqa: F811
):
    """The reason a Compose install could not switch itself at all.

    Cremind's own v0.0.1 Docker installer wrote ``APP_URL=http://localhost:1112``
    while that port was still published. The port map went away three releases
    later and nothing rewrites a deployment's ``.env`` on upgrade, so installs
    from that era still carry it — and the one thing this feature promises is
    that nobody has to go and edit that file.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112")
    monkeypatch.setattr(BaseConfig, "PORT", 1112)
    monkeypatch.setenv("APP_URL", "http://localhost:1112")
    value = prepared(client)

    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )

    assert result.status_code == 202, result.text
    written = overlay(compose).read_text(encoding="utf-8")
    assert "APP_URL=https://localhost:1515" in written
    assert "CREMIND_ATLASSIAN_REDIRECT_URI=https://localhost:1515/api/oauth/callback" in written
    assert result.json()["transition"]["app_url_repaired"] == {
        "from": "http://localhost:1112", "to": "https://localhost:1515",
    }

    # The next boot. A restart re-runs the entrypoint with the container's
    # creation-time environment, which still carries the stale value — so the
    # overlay has to keep outranking it rather than retiring itself.
    for key in managed.MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_URL", "http://localhost:1112")
    assert managed.is_redundant(managed.read(overlay(compose))) is False

    applied = managed.load_into_environ()

    assert applied["APP_URL"] == "https://localhost:1515"
    assert overlay(compose).exists(), "the deployment has not caught up yet"
