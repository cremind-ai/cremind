"""Recovery for an HTTPS switch that goes wrong.

Five layers, each catching what the one before it cannot:

* **R1** repairs a switch it can and refuses one it cannot, while nothing has
  changed yet.
* **R2** never exits into a crash loop over a change Cremind itself made.
* **R3** counts boots that were meant to serve HTTPS and did not.
* **R4** holds the credential boundary until a real client proves the new
  transport works.
* **R5** puts the installation back when that client never arrives.

R4 and R5 are the pair that matters most, and the only ones that can catch the
common real failure: a server that starts perfectly into an address nobody can
open — an untrusted CA, an unpublished port, a hostname missing from the SAN
set. Every layer before them is looking at this process, and this process is
fine.

The fixtures come from ``test_tls_transition`` so both files describe the same
installation; see the module docstring there.
"""
import asyncio
import json
import time

import pytest

from app.config import tls_mode, tls_transition as transition
from app.config.settings import BaseConfig
from app.server import _resolve_tls, _watch_https_confirmation

from tests.api.test_tls_transition import (  # noqa: F401 - fixtures by name
    auth,
    client,
    environment,
    prepared,
    served_https,
    storage_ready,
    ticket_body,
    token,
)


def self_applied(client, environment, monkeypatch, *, restart=True):
    """Activate the way a supervised native install does, and return the record.

    This is the shape every layer below is about: Cremind persisted the change
    and scheduled the restart, so it owns both halves of the undo. ``restart``
    False models ``--no-restart``, where the operator takes the timing back and
    the switch must stop being self-applied.
    """
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    monkeypatch.setattr("app.api.system.schedule_system_restart", lambda: 4321)
    (environment / ".env").write_text(
        "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n", encoding="utf-8",
    )
    value = prepared(client)
    body = {"transition_id": value["id"]}
    if not restart:
        body["restart"] = False
    result = client.post("/api/tls/activate", json=body, headers=auth())
    assert result.status_code == 202, result.text
    return transition.load_transition()


# ── R1: repair what can be repaired, refuse what cannot ───────────────────


def test_activation_repairs_an_app_url_naming_the_internal_bind(
    client, environment, monkeypatch,
):
    """The one input the switch derives rather than verifies.

    ``persist_native`` turns APP_URL into the HTTPS origin it writes into the
    agent card, the Google callback and the Atlassian callback. Port 1112 binds
    127.0.0.1 inside the container and is never published, so a switch built on
    it produces an installation whose every advertised address is unreachable —
    and says nothing at the time. Cremind's own first Docker installer wrote
    exactly that value, so this is a mistake it has to clean up rather than one
    to hand back: it owns this installation's environment, and refusing here
    would mean telling an administrator to go and edit a file by hand.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112")
    monkeypatch.setattr(BaseConfig, "PORT", 1112)
    value = prepared(client)

    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert result.status_code == 202, result.text

    written = (environment / ".env").read_text(encoding="utf-8")
    assert "APP_URL=https://localhost:1515" in written
    assert "CREMIND_ATLASSIAN_REDIRECT_URI=https://localhost:1515/api/oauth/callback" in written
    assert "1112" not in written
    assert BaseConfig.APP_URL == "https://localhost:1515"
    # Announced, because the address account linking now advertises is not the
    # one this installation was configured with.
    repaired = {"from": "http://localhost:1112", "to": "https://localhost:1515"}
    assert transition.load_transition()["app_url_repaired"] == repaired
    assert result.json()["transition"]["app_url_repaired"] == repaired
    # Repaired, not forced: the old value is in the rollback record, so the way
    # back is the same one every other activation has.
    record = json.loads(
        (environment / "tls" / "native-rollback.json").read_text(encoding="utf-8"))
    assert record["attrs"]["APP_URL"] == "http://localhost:1112"

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert not (environment / ".env").exists()
    assert BaseConfig.APP_URL == "http://localhost:1112"
    assert "app_url_repaired" not in transition.load_transition()


def test_a_public_origin_on_the_same_port_as_the_internal_bind_is_allowed(
    client, environment, monkeypatch,
):
    """A single-port install is not a misconfiguration.

    ``app_url_names_internal_bind`` only fires where the two ports genuinely
    differ; naming 1112 when 1112 *is* the public bind is just this install —
    so there is nothing to repair and nothing to report.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112")
    monkeypatch.setattr(BaseConfig, "PORT", 1112)
    monkeypatch.setenv("CREMIND_UI_PORT", "1112")
    value = prepared(client)

    allowed = client.post(
        "/api/tls/activate", json={"transition_id": value["id"], "restart": False},
        headers=auth(),
    )
    assert allowed.status_code == 202
    assert "APP_URL=https://localhost:1112" in (
        environment / ".env").read_text(encoding="utf-8")
    assert "app_url_repaired" not in transition.load_transition()


def test_an_external_deployment_is_still_refused(client, environment, monkeypatch):
    """Nothing to repair, because nothing is Cremind's to write.

    A chart or a reverse proxy holds its own environment; persisting a
    corrected APP_URL into a file this deployment never reads would report a
    fix that did not happen. So this is the one case that still has to be
    handed back — with the address to set, and where to set it.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112")
    monkeypatch.setattr(BaseConfig, "PORT", 1112)
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)

    refused = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )
    assert refused.status_code == 400
    assert "internal API bind" in refused.json()["error"]
    assert "1515" in refused.json()["error"]
    # Refused before anything moved: no phase change, no .env, no rollback record.
    assert transition.load_transition()["phase"] == "prepared"
    assert not (environment / ".env").exists()
    assert not (environment / "tls" / "native-rollback.json").exists()


def test_the_cli_prepares_the_public_port_on_a_stale_install(
    client, environment, monkeypatch,
):
    """The CLI reaches the internal port, so its origin comes from APP_URL.

    Which means a stale APP_URL would otherwise make ``cremind tls enable``
    prepare a switch *to* the internal port — the transition, the certificate's
    SAN set and the URL printed back all naming an address no browser can open.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1112")
    monkeypatch.setattr(BaseConfig, "PORT", 1112)

    result = client.post(
        f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/prepare", json={}, headers=auth(),
    )

    assert result.status_code == 200, result.text
    value = result.json()["transition"]
    assert value["source_origin"] == "http://localhost:1515"
    assert value["target_origin"] == "https://localhost:1515"
    assert client.get(
        f"http://127.0.0.1:{BaseConfig.PORT}/api/tls/status",
    ).json()["https_url"] == "https://localhost:1515"


# ── R2: never exit into a crash loop ──────────────────────────────────────


def test_an_operator_configured_certificate_still_fails_the_boot(
    client, environment, monkeypatch,
):
    """Nothing to undo, so the wrong answer is to serve plain HTTP quietly."""
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", str(environment / "nope.pem"))
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", str(environment / "nope.key"))

    with pytest.raises(SystemExit):
        _resolve_tls(None, None, 1515)


def test_a_certificate_failure_undoes_a_switch_cremind_applied(
    client, environment, monkeypatch,
):
    """The crash loop this layer exists to prevent.

    Under ``restart: unless-stopped`` an exit here produces a container with no
    listener at all — no HTTPS, no HTTP, no recovery page — over a change
    Cremind made and holds the record to unmake.
    """
    self_applied(client, environment, monkeypatch)
    assert (environment / "tls" / "native-rollback.json").exists()
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", str(environment / "nope.pem"))
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", str(environment / "nope.key"))

    assert _resolve_tls(None, None, 1515) is None  # serves plain HTTP instead

    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert "restored automatically" in stored["auto_reverted"]["reason"]
    assert stored["auto_reverted"]["restored"] is True
    # The installation is back, not merely marked as back.
    assert (environment / ".env").read_text() == (
        "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n"
    )
    assert BaseConfig.APP_URL == "http://testserver"
    assert not (environment / "tls" / "native-rollback.json").exists()


def test_a_certificate_that_cannot_be_generated_also_undoes_the_switch(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    monkeypatch.setattr(BaseConfig, "SSL_MODE", "auto")
    monkeypatch.setattr(
        "app.config.tls_auto.ensure_local_tls",
        lambda *_a: (_ for _ in ()).throw(OSError("read-only system directory")),
    )

    assert _resolve_tls(None, None, 1515) is None

    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert "read-only system directory" in stored["auto_reverted"]["reason"]


# ── R3: count boots that should have served HTTPS ─────────────────────────


def test_a_boot_that_should_have_served_https_and_did_not_is_counted(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)

    assert transition.reconcile_activation_boot(serving_https=False) is None

    stored = transition.load_transition()
    assert stored["failed_boots"] == 1
    assert stored["phase"] == "activating"  # one failure can be a slow volume


def test_two_failed_boots_put_the_installation_back(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)

    assert transition.reconcile_activation_boot(serving_https=False) is None
    reason = transition.reconcile_activation_boot(serving_https=False)

    assert reason is not None and "consecutive restarts" in reason
    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert (environment / ".env").read_text() == (
        "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n"
    )


def test_a_boot_that_serves_https_clears_the_counter(
    client, environment, monkeypatch,
):
    """Two *consecutive* failures, not two ever."""
    self_applied(client, environment, monkeypatch)
    transition.reconcile_activation_boot(serving_https=False)
    assert transition.load_transition()["failed_boots"] == 1

    transition.reconcile_activation_boot(serving_https=True)
    assert "failed_boots" not in transition.load_transition()

    assert transition.reconcile_activation_boot(serving_https=False) is None
    assert transition.load_transition()["phase"] == "activating"


def test_a_switch_waiting_for_an_operator_is_never_counted(
    client, environment, monkeypatch,
):
    """A Helm switch is *supposed* to sit through restarts.

    It waits until the operator applies the deployment change, which may be
    tomorrow. Counting those boots would revert a switch that is proceeding
    exactly as designed — and Cremind holds no rollback record for it anyway.
    """
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).status_code == 202
    assert transition.load_transition().get("self_applied") is not True

    for _ in range(5):
        assert transition.reconcile_activation_boot(serving_https=False) is None

    stored = transition.load_transition()
    assert stored["phase"] == "activating"
    assert "failed_boots" not in stored


def test_no_restart_means_the_operator_owns_the_timing(
    client, environment, monkeypatch,
):
    """``--no-restart`` must not be reverted underneath the person restarting."""
    stored = self_applied(client, environment, monkeypatch, restart=False)
    assert stored.get("self_applied") is not True
    assert "confirmation_deadline" not in stored

    for _ in range(5):
        assert transition.reconcile_activation_boot(serving_https=False) is None
    assert transition.load_transition()["phase"] == "activating"


# ── R4: hold the boundary until a client proves the transport ─────────────


def test_the_boundary_waits_for_a_client_on_a_self_applied_switch(
    client, environment, monkeypatch,
):
    """Binding TLS proves nothing about whether a browser can reach it."""
    alice = token("alice")
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    transition.mark_active(confirmed=False)

    stored = transition.load_transition()
    assert stored["phase"] == "activating"
    assert stored["pending_transport_epoch"] == 1
    assert "transport_epoch" not in stored
    from app.auth.tokens import verify_token
    assert verify_token(alice) is not None  # nothing invalidated yet
    assert (environment / "tls" / "native-rollback.json").exists()  # still undoable


def test_an_authenticated_https_request_confirms_the_switch(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    answered = client.get("https://testserver:80/api/tls/status", headers=auth())
    assert answered.status_code == 200

    stored = transition.load_transition()
    assert stored["phase"] == "active"
    assert stored["transport_epoch"] == 1
    assert "pending_transport_epoch" not in stored
    # Past the point of no return, the way back is deliberately gone.
    assert not (environment / "tls" / "native-rollback.json").exists()


def test_an_unauthenticated_https_poll_does_not_confirm(
    client, environment, monkeypatch,
):
    """The recovery page polls this endpoint cross-origin with credentials
    omitted. That proves TLS reachability from *some* client, not that the
    administrator can still use their installation."""
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    assert client.get("https://testserver:80/api/tls/status").status_code == 200

    stored = transition.load_transition()
    assert stored["phase"] == "activating"
    assert stored["pending_transport_epoch"] == 1


def test_a_non_admin_https_request_does_not_confirm(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    assert client.get(
        "https://testserver:80/api/tls/status", headers=auth("alice"),
    ).status_code == 200
    assert "pending_transport_epoch" in transition.load_transition()


def test_a_redeemed_handoff_confirms_the_switch(client, environment, monkeypatch):
    """A browser that completed the handshake and carried a session across."""
    value = prepared(client)
    ticket = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")
    monkeypatch.setattr("app.api.system.schedule_system_restart", lambda: 4321)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).status_code == 202
    assert transition.load_transition()["self_applied"] is True
    served_https(monkeypatch)

    redeemed = client.post(
        "https://testserver:80/api/tls/handoff/redeem", json={"ticket": ticket},
    )
    assert redeemed.status_code == 200, redeemed.text

    stored = transition.load_transition()
    assert stored["phase"] == "active"
    assert "pending_transport_epoch" not in stored


def test_a_ticketless_redeem_cannot_confirm_the_switch(
    client, environment, monkeypatch,
):
    """The redeem route is unauthenticated, so the ticket has to be the proof.

    Its only other precondition is an https scheme, which is satisfied by any
    client that skips certificate validation — and, on the internal loopback
    bind, by a forged X-Forwarded-Proto from any local process. An empty body
    would otherwise advance the boundary, delete the rollback record and
    disarm the watchdog: every way back from a switch nobody can reach, gone.
    """
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    answered = client.post("https://testserver:80/api/tls/handoff/redeem", json={})
    assert answered.status_code == 400

    stored = transition.load_transition()
    assert stored["phase"] == "activating"
    assert stored["pending_transport_epoch"] == 1
    assert (environment / "tls" / "native-rollback.json").exists()
    assert transition.plaintext_may_serve_app() is True


def test_a_bogus_ticket_cannot_confirm_the_switch(client, environment, monkeypatch):
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)

    assert client.post(
        "https://testserver:80/api/tls/handoff/redeem", json={"ticket": "x" * 43},
    ).status_code == 400
    assert "pending_transport_epoch" in transition.load_transition()


def test_an_expired_ticket_still_proves_the_origin_works(
    client, environment, monkeypatch,
):
    """Existence, not validity: holding a minted ticket proves a real client
    reached the new origin, and re-signing in there is not a failure."""
    value = prepared(client)
    ticket = client.post(
        "/api/tls/handoff", json=ticket_body(value), headers=auth("alice"),
    ).json()["ticket"]
    assert transition.ticket_exists(ticket) is True
    assert transition.ticket_exists("x" * 43) is False
    assert transition.ticket_exists(None) is False
    assert transition.ticket_exists("not-a-ticket") is False


def test_a_deployment_managed_switch_still_advances_unconfirmed(
    client, environment, monkeypatch,
):
    """No behaviour change where Cremind could not undo the switch anyway.

    Waiting would buy a Docker or Helm install nothing — there is no rollback
    record to apply at the deadline — and would leave every HTTP-era session
    alive for ten minutes longer than it is today.
    """
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).status_code == 202
    served_https(monkeypatch)

    transition.mark_active(confirmed=False)

    stored = transition.load_transition()
    assert stored["phase"] == "active"
    assert stored["transport_epoch"] == 1


# ── R5: put it back when nobody ever arrives ──────────────────────────────


def test_an_unconfirmed_switch_reverts_at_its_deadline(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)
    overdue = transition.load_transition()
    overdue["confirmation_deadline"] = time.time() - 1
    transition.save_transition(overdue)
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 9,
    )

    asyncio.run(_watch_https_confirmation())

    stored = transition.load_transition()
    assert stored["phase"] == "cancelled"
    assert "no browser reached it" in stored["auto_reverted"]["reason"]
    assert (environment / ".env").read_text() == (
        "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n"
    )
    # The reverted configuration only takes effect on a fresh process.
    assert restarts == [True]


def test_the_watchdog_stops_as_soon_as_the_switch_is_confirmed(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)
    assert client.get(
        "https://testserver:80/api/tls/status", headers=auth(),
    ).status_code == 200
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 9,
    )

    asyncio.run(asyncio.wait_for(_watch_https_confirmation(), timeout=5))

    assert transition.load_transition()["phase"] == "active"
    assert restarts == []


def test_the_watchdog_ignores_a_switch_that_was_never_ours(
    client, environment, monkeypatch,
):
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    overdue = transition.load_transition()
    overdue["confirmation_deadline"] = time.time() - 1
    transition.save_transition(overdue)

    asyncio.run(asyncio.wait_for(_watch_https_confirmation(), timeout=5))

    assert transition.load_transition()["phase"] == "activating"


def test_auto_revert_refuses_once_the_boundary_has_moved(
    client, environment, monkeypatch,
):
    """The promise cancel makes, kept by the unattended path too."""
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)
    client.get("https://testserver:80/api/tls/status", headers=auth())
    assert "pending_transport_epoch" not in transition.load_transition()

    assert transition.auto_revert("too late") is False
    assert transition.load_transition()["phase"] == "active"


# ── the way back stays open while the switch is unconfirmed ───────────────


def test_plaintext_serves_the_application_while_the_switch_is_unconfirmed(
    client, environment, monkeypatch,
):
    """Otherwise the administrator has no authenticated surface to cancel from.

    The relay used to hand every plaintext request to the recovery page the
    moment the process bound TLS — including the request from the one tab that
    could still call the switch off.
    """
    self_applied(client, environment, monkeypatch)
    assert transition.plaintext_may_serve_app() is True

    served_https(monkeypatch)
    client.get("https://testserver:80/api/tls/status", headers=auth())

    assert transition.load_transition()["phase"] == "active"
    assert transition.plaintext_may_serve_app() is False


def test_cancel_stays_available_while_a_self_applied_switch_is_unconfirmed(
    client, environment, monkeypatch,
):
    self_applied(client, environment, monkeypatch)
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    # HTTPS is bound, but nobody has reached it — exactly when the way back matters.
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    storage_ready(monkeypatch)

    assert client.get("/api/tls/status").json()["can_cancel"] is True
    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )
    assert cancelled.status_code == 200
    assert (environment / ".env").read_text() == (
        "KEEP_ME=one\nCREMIND_SSL=false\nAPP_URL=http://testserver\n"
    )


def test_cancelling_a_switch_this_process_serves_restarts_onto_http(
    client, environment, monkeypatch,
):
    """Cancel is reachable on a process that has already bound TLS, and that
    process cannot unbind it. Without the restart the cancel would revert the
    configuration, leave HTTPS serving anyway, and close the plaintext surface
    the request arrived on — leaving the administrator worse off than before
    they pressed the button."""
    self_applied(client, environment, monkeypatch)
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    storage_ready(monkeypatch)
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 7,
    )

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["restart_scheduled"] is True
    assert restarts == [True]


def test_cancelling_before_https_is_bound_schedules_nothing(
    client, environment, monkeypatch,
):
    """The ordinary cancel, where the process never bound TLS, is unchanged."""
    self_applied(client, environment, monkeypatch)
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 7,
    )

    cancelled = client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["restart_scheduled"] is False
    assert restarts == []


def test_a_confirmation_stops_the_deadline_even_if_the_advance_fails(
    client, environment, monkeypatch,
):
    """The deadline asks "can anyone reach this?". Once answered it must stop.

    Otherwise a switch a browser demonstrably reached is reverted underneath
    the administrator already using it, because re-signing the token files
    happened to fail.
    """
    self_applied(client, environment, monkeypatch)
    served_https(monkeypatch)
    monkeypatch.setattr(
        "app.auth.tokens.reissue_token_files_for_epoch",
        lambda _epoch: (_ for _ in ()).throw(OSError("token directory is read-only")),
    )

    client.get("https://testserver:80/api/tls/status", headers=auth())

    stored = transition.load_transition()
    assert stored["phase"] == "activating"  # the advance genuinely failed
    assert stored["activation_error"]
    assert "confirmation_deadline" not in stored  # but the clock has stopped
    restarts = []
    monkeypatch.setattr(
        "app.api.system.schedule_system_restart", lambda: restarts.append(True) or 7,
    )
    asyncio.run(asyncio.wait_for(_watch_https_confirmation(), timeout=5))
    assert transition.load_transition()["phase"] == "activating"
    assert restarts == []


def test_a_deployment_managed_install_still_serving_http_may_still_cancel(
    client, environment, monkeypatch,
):
    """The 409 that told an operator HTTPS was serving while HTTP served them.

    ``CREMIND_SSL`` never took effect but ``APP_URL`` was already https, and
    cancel read that as proof the switch had landed — refusing the one action
    that would have given the installation back.
    """
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    assert client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    ).status_code == 202
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://testserver:1515")

    assert client.get("/api/tls/status").json()["can_cancel"] is True
    assert client.post(
        "/api/tls/cancel", json={"transition_id": stale["id"]}, headers=auth(),
    ).status_code == 200


def test_a_reverse_proxy_install_still_reads_app_url_as_proof(
    client, environment, monkeypatch,
):
    """Where the process holds no public bind it genuinely cannot observe the
    transport, so the configured origin is the only evidence there is."""
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    value = prepared(client)
    client.post("/api/tls/activate", json={"transition_id": value["id"]}, headers=auth())
    stale = transition.load_transition()
    stale["activated_at"] = time.time() - transition.RESTART_GRACE_SECONDS - 1
    transition.save_transition(stale)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example")
    monkeypatch.setenv("CREMIND_UI_PORT", "0")

    assert client.get("/api/tls/status").json()["can_cancel"] is False


# ── what the UI is told ───────────────────────────────────────────────────


def test_status_publishes_the_deadline_and_the_reversal(
    client, environment, monkeypatch,
):
    """A tab that cannot see either reports "still waiting" forever, and then
    reports a switch that already undid itself as merely failed."""
    self_applied(client, environment, monkeypatch)

    waiting = client.get("/api/tls/status").json()["transition"]
    assert waiting["confirmation_deadline"] > time.time()
    assert waiting["auto_reverted"] is None

    transition.auto_revert("the deadline passed")

    reverted = client.get("/api/tls/status").json()["transition"]
    assert reverted["phase"] == "cancelled"
    assert reverted["auto_reverted"]["reason"] == "the deadline passed"
    assert reverted["confirmation_deadline"] is None
    assert json.dumps(reverted)  # the whole record stays JSON-serialisable


def test_an_unset_app_url_takes_the_address_the_admin_actually_reached(
    client, environment, monkeypatch,
):
    """A Compose ``.env`` missing the key hands the container ``APP_URL=``.

    There is no hostname to keep then, and ``localhost`` would be a guess — on
    a server install a wrong one, baked into the agent card and both OAuth
    callbacks. The origins that joined this switch are the addresses the
    administrator authenticated through, and CORS and the SAN set are already
    built from them.
    """
    monkeypatch.setattr(BaseConfig, "APP_URL", "")
    value = prepared(client)

    result = client.post(
        "/api/tls/activate", json={"transition_id": value["id"]}, headers=auth(),
    )

    assert result.status_code == 202, result.text
    written = (environment / ".env").read_text(encoding="utf-8")
    assert "APP_URL=https://testserver:80" in written
    assert "localhost" not in written
    assert result.json()["transition"]["app_url_repaired"] == {
        "from": "", "to": "https://testserver:80",
    }
