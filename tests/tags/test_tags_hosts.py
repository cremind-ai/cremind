"""Gateway computers (:mod:`app.tags.hosts`): who may use a computer's USB
ports, searching one, connecting a gateway found there, recovering a
connection onto another computer, and the narrow API a host reports to.

The hosts are driven the way the hardware runtime drives them (status report,
take work, report progress, register the worker); the worker then claims the
gateway through the real connector API (the helpers of
``test_tags_setup.py``). Two profiles take part throughout: nothing one of
them searched, found or connected is ever visible to or usable by the other.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import text

from tests.tags._helpers import find_handler, run, scalar, tagenv  # noqa: F401
from tests.tags.test_tags_setup import Device, Worker, _pub, call, grant_for, req

SRV_ID = "host-srv"


@pytest.fixture(autouse=True)
def _authority_dir(tmp_path, monkeypatch):
    from app.tags import authority

    monkeypatch.setattr(authority, "directory", lambda: tmp_path / ".tag-authority")
    monkeypatch.setenv("CREMIND_TAGS_SIMPLE_SETUP", "1")
    authority.reset_cache()
    yield
    authority.reset_cache()


def server():
    from app.tags import hosts

    return hosts.HostPrincipal(SRV_ID, hosts.SERVER)


def hello(principal=None, *, name: str = "Office PC", state: str = "running", reason: str | None = None) -> dict:
    from app.tags import hosts

    return run(hosts.host_hello(principal or server(), {
        "name": name, "platform": "windows", "version": "0.0.19",
        "capabilities": {"readiness": {"state": "ready", "components": []},
                         "usb": {"available": True, "container": False, "reason": None}},
        "status": {"state": state, "reason": reason, "gateways": [], "workers": []}}))


def api(method: str, path: str, profile: str | None, *, params: dict | None = None,
        body: Any = None) -> tuple[int, dict]:
    from app.api.tags_hosts import get_tags_hosts_routes

    return call(get_tags_hosts_routes(), method, path, req(profile, path=params, body=body))


def connections(profile: str) -> list[dict]:
    from app.api.tags_setup import get_tags_setup_routes

    status, out = call(get_tags_setup_routes(), "GET", "/api/tags/connections", req(profile))
    assert status == 200, out
    return out["connections"]


def host_view(profile: str, host_id: str = SRV_ID) -> dict | None:
    status, out = api("GET", "/api/tags/hosts", profile)
    assert status == 200, out
    return next((h for h in out["hosts"] if h["id"] == host_id), None)


def operation(profile: str, op_id: str) -> tuple[int, dict]:
    return api("GET", "/api/tags/operations/{op_id}", profile, params={"op_id": op_id})


def found(device: Device, **extra) -> dict[str, Any]:
    """A gateway as the host's probe reports it (never a port)."""
    return {"device_id": device.device_id, "ik": device.ik, "role": "gateway", "proto": 2, "fw": "0.2.0",
            "board": 19, "owner_state": "unowned", "gen": 0, "authority_id": None, **extra}


def grant(profile_id: str, granted: bool, *, caller: str = "admin", host_id: str = SRV_ID) -> tuple[int, dict]:
    return api("PUT", "/api/tags/hosts/{host_id}/access/{profile_id}", caller,
               params={"host_id": host_id, "profile_id": profile_id}, body={"granted": granted})


def search(profile: str, devices: list[Device], *, principal=None, host_id: str = SRV_ID,
           unidentified: tuple = ()) -> dict:
    """Search a computer: the profile asks, the host takes the work and reports what it found."""
    from app.tags import hosts

    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", profile, params={"host_id": host_id}, body={})
    assert status == 202, out
    op_id = out["operation"]["id"]
    assert out["operation"]["kind"] == "host_scan" and out["operation"]["host_id"] == host_id
    work = run(hosts.host_work(principal or server(), 0))["work"]
    assert [w["id"] for w in work] == [op_id]
    run(hosts.host_progress(principal or server(), op_id, {
        "state": "succeeded", "found": [found(d) for d in devices], "unidentified": list(unidentified)}))
    status, out = operation(profile, op_id)
    assert status == 200 and out["operation"]["state"] == "succeeded", out
    return out["operation"]


def candidate_for(op: dict, device: Device) -> dict:
    return next(c for c in op["candidates"] if c["device_id"] == device.device_id)


def connect(profile: str, candidate: dict, gateway: Device, *, principal=None, host_id: str = SRV_ID,
            name: str | None = None, recover: bool = False) -> SimpleNamespace:
    """Connect a found gateway: the host takes the connection, checks the gateway again and registers
    the worker it prepared (controller key, two credential secrets)."""
    from app.tags import credentials as creds
    from app.tags import hosts

    body = {"host_id": host_id, "candidate_id": candidate["id"], **({"name": name} if name else {}),
            **({"recover": True} if recover else {})}
    status, out = api("POST", "/api/tags/connections", profile, body=body)
    assert status == 202, out
    op_id = out["operation"]["id"]
    work = run(hosts.host_work(principal or server(), 0))["work"]
    [item] = [w for w in work if w["id"] == op_id]
    assert item["kind"] == "host_connect" and item["args"]["device_id"] == gateway.device_id
    assert item["args"]["ik"] == gateway.ik and "port" not in item["args"]
    controller = X25519PrivateKey.generate()
    hw, ct = creds.new_secret(), creds.new_secret()
    registered = run(hosts.host_register_worker(principal or server(), op_id, {
        "controller_pub": _pub(controller).hex(),
        "credentials": {"hardware_sha256": creds.hash_secret(hw), "content_sha256": creds.hash_secret(ct)},
        "gateway": {"device_id": gateway.device_id, "ik": gateway.ik, "gen": 0, "proto": 2, "fw": "0.2.0"}}))
    worker = Worker(f"CremindTag {registered['credentials']['hardware_id']}.{hw}",
                    f"CremindTag {registered['credentials']['content_id']}.{ct}", controller,
                    registered["companion_id"])
    return SimpleNamespace(op_id=op_id, registered=registered, worker=worker, claim_id=registered["operation_id"],
                           gateway=gateway, companion_id=registered["companion_id"], controller=controller)


def finish_claim(ctx) -> None:
    """The worker claims the gateway and reports in: the connection is made."""
    status, g = grant_for(ctx.worker, ctx.claim_id, ctx.gateway, "claim")
    assert status == 200, g
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": ctx.claim_id},
                                  body={"stage": "claimed", "state": "succeeded", "device": {"gen": 1}})
    assert status == 200, out
    status, out = ctx.worker.call("POST", "/heartbeat", body={"companion": {"version": "0.0.19"}})
    assert status == 200, out


def desktop(env, profile: str, profile_id: str, *, host_id: str = "host-dsk", name: str = "Laptop"):
    """An enrolled desktop computer of ``profile``, with its host credential."""
    from app.tags import credentials as creds
    from app.tags import hosts

    cred_id, secret = hosts.new_host_credential()
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO tag_hosts (id, kind, name, owner_profile, owner_profile_id, state, created_at, "
                       "updated_at) VALUES (:i, 'desktop', :n, :p, :pid, 'active', 0, 0)"),
                  {"i": host_id, "n": name, "p": profile, "pid": profile_id})
        c.execute(text("INSERT INTO tag_host_credentials (id, host_id, profile, profile_id, secret_sha256, "
                       "created_at) VALUES (:i, :h, :p, :pid, :s, 0)"),
                  {"i": cred_id, "h": host_id, "p": profile, "pid": profile_id, "s": creds.hash_secret(secret)})
    principal = run(hosts.authenticate_host(cred_id, secret))
    return principal, cred_id, secret


# ---------------------------------------------------------------- the server's own computer


def test_the_admin_connects_a_gateway_on_the_server_end_to_end(tagenv) -> None:
    hello()
    host = host_view("admin")
    assert host["kind"] == "server" and host["name"] == "Office PC" and host["online"] and host["state"] == "running"
    assert host["access"]["can_use"] and host["access"]["can_manage"]
    assert [(p["profile"], p["granted"]) for p in host["access"]["profiles"]] == [("p1", False), ("p2", False)]
    assert host["readiness"]["state"] == "ready" and host["usb"]["available"] is True

    gw = Device("gateway")
    op = search("admin", [gw], unidentified=({"reason": "busy", "detail": "in use by another program"},))
    [cand] = op["candidates"]
    assert cand["state"] == "usable" and cand["id"].startswith("hc_") and cand["short_id"]
    assert not {"ik", "expires_ms", "gen", "mode", "port"} & set(cand), "a candidate names no key and no port"
    assert op["ports"] == [{"reason": "busy", "detail": "in use by another program"}]

    ctx = connect("admin", cand, gw, name="Office gateway")
    assert ctx.registered["profile"] == {"name": "admin", "id": "pid0"}
    assert scalar(tagenv, "SELECT state FROM tag_companions WHERE id = :c", c=ctx.companion_id) == "connecting"
    assert scalar(tagenv, "SELECT execution_kind FROM tag_companions WHERE id = :c", c=ctx.companion_id) == "server"
    assert connections("admin") == [], "a connection appears only once its gateway is claimed"
    status, out = operation("admin", ctx.op_id)
    assert out["operation"]["state"] == "running" and out["operation"]["stage"] == "claiming"
    assert out["operation"]["companion_id"] == ctx.companion_id

    finish_claim(ctx)
    status, out = operation("admin", ctx.op_id)
    assert out["operation"]["state"] == "succeeded", out
    [conn] = connections("admin")
    assert conn["id"] == ctx.companion_id and conn["status"] == "connected" and conn["name"] == "Office gateway"
    assert conn["execution_kind"] == "server" and conn["host_id"] == SRV_ID
    assert conn["computer"]["kind"] == "server" and conn["computer"]["name"] == "Office PC"
    assert host_view("admin")["connections"] == 1

    # Registering again (a retried request) answers the same records; another worker is refused.
    from app.tags import credentials as creds
    from app.tags import hosts
    from app.tags.service import TagError

    again = run(hosts.host_register_worker(server(), ctx.op_id, {
        "controller_pub": ctx.worker.controller_pub,
        "credentials": {"hardware_sha256": "1" * 64, "content_sha256": "2" * 64},
        "gateway": {"device_id": gw.device_id, "ik": gw.ik}}))
    assert again["companion_id"] == ctx.companion_id
    with pytest.raises(TagError) as info:
        run(hosts.host_register_worker(server(), ctx.op_id, {
            "controller_pub": _pub(X25519PrivateKey.generate()).hex(),
            "credentials": {"hardware_sha256": creds.hash_secret("a"), "content_sha256": creds.hash_secret("b")},
            "gateway": {"device_id": gw.device_id, "ik": gw.ik}}))
    assert info.value.code == "already_registered"

    # Searching again: the gateway is connected here now.
    assert search("admin", [gw])["candidates"][0]["state"] == "already_connected"


def test_profiles_need_the_admins_grant_and_never_see_each_others_hardware(tagenv) -> None:
    hello()
    host = host_view("p1")
    assert host is not None and not host["access"]["can_use"] and not host["access"]["can_manage"]
    assert "admin" in host["access"]["reason"] and "profiles" not in host["access"]
    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "p1", params={"host_id": SRV_ID}, body={})
    assert status == 403 and out["error"] == "host_access_denied"
    status, out = grant("pid2", True, caller="p1")
    assert status == 403 and out["error"] == "admin_required"
    status, out = grant("pid-unknown", True)
    assert status == 404 and out["error"] == "profile_not_found"

    status, out = grant("pid1", True)
    assert status == 200 and out == {"host_id": SRV_ID, "profile_id": "pid1", "profile": "p1", "granted": True}
    assert [(p["profile"], p["granted"]) for p in host_view("admin")["access"]["profiles"]] == \
        [("p1", True), ("p2", False)]
    assert host_view("p1")["access"]["can_use"]

    # The admin's gateway, connected.
    gw_admin, gw_p1 = Device("gateway"), Device("gateway")
    ctx = connect("admin", search("admin", [gw_admin])["candidates"][0], gw_admin)
    finish_claim(ctx)

    # p1 finds both: the admin's is someone else's, its own is free.
    op = search("p1", [gw_admin, gw_p1])
    assert {c["device_id"]: c["state"] for c in op["candidates"]} == \
        {gw_admin.device_id: "owned_elsewhere", gw_p1.device_id: "usable"}
    assert candidate_for(op, gw_admin)["companion_id"] is None, "never another profile's connection id"
    status, out = api("POST", "/api/tags/connections", "p1",
                      body={"host_id": SRV_ID, "candidate_id": candidate_for(op, gw_admin)["id"]})
    assert status == 409 and out["error"] == "owned_elsewhere"

    # p2 can neither read p1's search nor use what it found.
    status, out = operation("p2", op["id"])
    assert status == 404
    status, out = api("POST", "/api/tags/connections", "p2",
                      body={"host_id": SRV_ID, "candidate_id": candidate_for(op, gw_p1)["id"]})
    assert status == 403 and out["error"] == "host_access_denied"
    grant("pid2", True)
    status, out = api("POST", "/api/tags/connections", "p2",
                      body={"host_id": SRV_ID, "candidate_id": candidate_for(op, gw_p1)["id"]})
    assert status == 404 and out["error"] == "candidate_not_found"

    # p1 connects its own; the admin sees nothing of it, nor p1 anything of the admin's.
    ctx1 = connect("p1", candidate_for(op, gw_p1), gw_p1)
    finish_claim(ctx1)
    assert [c["id"] for c in connections("p1")] == [ctx1.companion_id]
    assert [c["id"] for c in connections("admin")] == [ctx.companion_id]
    assert host_view("p1")["connections"] == 1 and host_view("admin")["connections"] == 1

    # Revoking the grant stops new searches; one already queued fails when the host takes it.
    from app.tags import hosts

    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "p1", params={"host_id": SRV_ID}, body={})
    assert status == 202
    queued = out["operation"]["id"]
    assert grant("pid1", False)[0] == 200
    assert run(hosts.host_work(server(), 0))["work"] == []
    status, out = operation("p1", queued)
    assert out["operation"]["state"] == "failed" and out["operation"]["error"]["code"] == "host_access_denied"
    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "p1", params={"host_id": SRV_ID}, body={})
    assert status == 403
    # ... and leaves p1's own connection alone.
    assert [c["id"] for c in connections("p1")] == [ctx1.companion_id]


def test_a_search_result_expires_and_a_connection_names_only_a_candidate(tagenv, monkeypatch) -> None:
    from app.tags import hosts

    hello()
    gw = Device("gateway")
    [cand] = search("admin", [gw])["candidates"]
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": SRV_ID})
    assert status == 422 and out["error"] == "invalid_connection"
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": SRV_ID, "candidate_id": "hc_nope"})
    assert status == 404 and out["error"] == "candidate_not_found"
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": "elsewhere", "candidate_id": cand["id"]})
    assert status == 404 and out["error"] == "host_not_found"

    later = hosts.now_ms() + hosts.CANDIDATE_TTL_MS + 1_000
    monkeypatch.setattr(hosts, "now_ms", lambda: later)
    hello()  # the server keeps reporting in
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": SRV_ID, "candidate_id": cand["id"]})
    assert status == 410 and out["error"] == "candidate_expired"


def test_a_computer_that_cannot_search_says_why(tagenv, monkeypatch) -> None:
    from app.tags import hosts

    def scan_error() -> str:
        status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "admin", params={"host_id": SRV_ID}, body={})
        assert status == 409, out
        return out["error"]

    hello(state="unavailable", reason="Gateway components are missing.")
    assert host_view("admin")["state"] == "unavailable"
    assert scan_error() == "components_unavailable"
    hello(state="busy_elsewhere", reason="Another Cremind process on this computer runs the gateways.")
    assert scan_error() == "host_busy"
    hello(state="failed", reason="The hardware runtime stopped with an error (see the log).")
    assert scan_error() == "host_not_running"
    hello()
    later = hosts.now_ms() + hosts.ONLINE_WINDOW_MS + 1_000
    monkeypatch.setattr(hosts, "now_ms", lambda: later)
    assert host_view("admin")["state"] == "stalled"
    assert scan_error() == "host_not_running"

    # A desktop computer that stopped reporting is offline.
    principal, _, _ = desktop(tagenv, "p1", "pid1")
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_hosts SET last_seen_at = :t, status = NULL WHERE id = 'host-dsk'"),
                  {"t": later - hosts.ONLINE_WINDOW_MS * 3})
    view = host_view("p1", "host-dsk")
    assert view["online"] is False
    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "p1", params={"host_id": "host-dsk"}, body={})
    assert status == 409 and out["error"] == "host_offline"


# ---------------------------------------------------------------- connections that do not finish


def test_a_refused_claim_fails_the_connection_and_leaves_nothing_behind(tagenv) -> None:
    hello()
    gw = Device("gateway")
    ctx = connect("admin", search("admin", [gw])["candidates"][0], gw)
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": ctx.claim_id},
                                  body={"state": "failed", "error": {"code": "grant_refused",
                                                                     "message": "The device refused."}})
    assert status == 200, out
    status, out = operation("admin", ctx.op_id)
    assert out["operation"]["state"] == "failed" and out["operation"]["error"]["code"] == "grant_refused"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_companions WHERE id = :c", c=ctx.companion_id) == 0
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_bindings WHERE device_id = :d", d=gw.device_id) == 0
    status, _ = ctx.worker.call("GET", "/whoami")
    assert status == 401, "the abandoned worker's credentials no longer work"
    assert connections("admin") == []
    # The gateway can be connected again.
    assert search("admin", [gw])["candidates"][0]["state"] == "usable"


def test_connections_past_their_deadline_end(tagenv) -> None:
    from app.tags import hosts

    hello()
    gw1, gw2 = Device("gateway"), Device("gateway")
    op = search("admin", [gw1, gw2])
    # Registered but never claimed: its worker records go with it.
    ctx = connect("admin", candidate_for(op, gw2), gw2)
    # Queued and never taken: the computer did not pick it up.
    status, out = api("POST", "/api/tags/connections", "admin",
                      body={"host_id": SRV_ID, "candidate_id": candidate_for(op, gw1)["id"]})
    assert status == 202
    waiting = out["operation"]["id"]
    later = hosts.now_ms() + hosts.CONNECT_TTL_S * 1000 + 1_000
    assert run(hosts.expire(later)) >= 2
    status, out = operation("admin", waiting)
    assert out["operation"]["state"] == "failed" and out["operation"]["error"]["code"] == "host_offline"
    status, out = operation("admin", ctx.op_id)
    assert out["operation"]["state"] == "failed" and out["operation"]["error"]["code"] == "timeout"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_companions WHERE id = :c", c=ctx.companion_id) == 0


def test_cancelling_a_connection_before_the_claim(tagenv) -> None:
    hello()
    gw = Device("gateway")
    [cand] = search("admin", [gw])["candidates"]
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": SRV_ID, "candidate_id": cand["id"]})
    op_id = out["operation"]["id"]
    status, out = api("DELETE", "/api/tags/operations/{op_id}", "admin", params={"op_id": op_id})
    assert status == 200 and out["operation"]["state"] == "cancelled"
    status, out = api("DELETE", "/api/tags/operations/{op_id}", "p1", params={"op_id": op_id})
    assert status == 404


# ---------------------------------------------------------------- desktop computers and recovery


def test_a_desktop_computer_is_its_owners_alone(tagenv) -> None:
    from app.tags import hosts
    from app.tags.service import TagError

    principal, cred_id, secret = desktop(tagenv, "p1", "pid1")
    assert (principal.host_id, principal.kind, principal.profile, principal.profile_id) == \
        ("host-dsk", "desktop", "p1", "pid1")
    hello(principal, name="Laptop")
    assert host_view("p1", "host-dsk")["access"] == {"can_use": True, "reason": None, "can_manage": False}
    assert host_view("p2", "host-dsk") is None and host_view("admin", "host-dsk") is None
    for profile in ("p2", "admin"):
        status, out = api("POST", "/api/tags/hosts/{host_id}/scan", profile, params={"host_id": "host-dsk"}, body={})
        assert status == 404 and out["error"] == "host_not_found"
    status, out = grant("pid2", True, host_id="host-dsk")
    assert status == 409 and out["error"] == "host_not_shareable"

    gw = Device("gateway")
    op = search("p1", [gw], principal=principal, host_id="host-dsk")
    ctx = connect("p1", op["candidates"][0], gw, principal=principal, host_id="host-dsk")
    finish_claim(ctx)
    [conn] = connections("p1")
    assert conn["execution_kind"] == "desktop" and conn["computer"]["name"] == "Laptop"

    # The credential: exact, revocable, bound to its profile.
    with pytest.raises(TagError) as info:
        run(hosts.authenticate_host(cred_id, secret[:-1] + ("A" if secret[-1] != "A" else "B")))
    assert info.value.code == "invalid_credential"
    assert hosts.parse_host_authorization(f"CremindHost {cred_id}.{secret}") == (cred_id, secret)
    assert hosts.parse_host_authorization(f"Bearer {cred_id}.{secret}") is None
    assert hosts.parse_host_authorization("CremindHost tagh_x.short") is None
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_host_credentials SET revoked_at = 1 WHERE id = :i"), {"i": cred_id})
    with pytest.raises(TagError) as info:
        run(hosts.authenticate_host(cred_id, secret))
    assert info.value.code == "credential_revoked"


def test_a_host_reports_only_on_its_own_work(tagenv) -> None:
    from app.tags import hosts
    from app.tags.service import TagError

    hello()
    principal, _, _ = desktop(tagenv, "admin", "pid0")
    hello(principal, name="Laptop")
    status, out = api("POST", "/api/tags/hosts/{host_id}/scan", "admin", params={"host_id": SRV_ID}, body={})
    op_id = out["operation"]["id"]
    assert run(hosts.host_work(principal, 0))["work"] == [], "another computer's search is not handed out"
    with pytest.raises(TagError) as info:
        run(hosts.host_progress(principal, op_id, {"state": "succeeded", "found": []}))
    assert info.value.code == "operation_not_found"
    with pytest.raises(TagError) as info:
        run(hosts.host_register_worker(principal, op_id, {
            "controller_pub": "ab" * 32, "credentials": {"hardware_sha256": "1" * 64, "content_sha256": "2" * 64}}))
    assert info.value.code == "operation_not_found"


def test_recovering_moves_a_connection_to_another_computer(tagenv) -> None:
    from app.api.tags_setup import get_tags_setup_routes

    hello()
    principal, _, _ = desktop(tagenv, "admin", "pid0")
    hello(principal, name="Laptop")
    gw = Device("gateway")
    ctx = connect("admin", search("admin", [gw])["candidates"][0], gw)
    finish_claim(ctx)
    generation = scalar(tagenv, "SELECT generation FROM tag_companions WHERE id = :c", c=ctx.companion_id)

    # Plugged into the laptop: the admin's own gateway, driven from another computer.
    op = search("admin", [gw], principal=principal, host_id="host-dsk")
    [cand] = op["candidates"]
    assert cand["state"] == "recovery_required" and cand["companion_id"] == ctx.companion_id
    status, out = api("POST", "/api/tags/connections", "admin", body={"host_id": "host-dsk", "candidate_id": cand["id"]})
    assert status == 409 and out["error"] == "recovery_required"

    moved = connect("admin", cand, gw, principal=principal, host_id="host-dsk", recover=True)
    assert moved.companion_id == ctx.companion_id and moved.registered["recovery_operation_id"]
    status, out = operation("admin", moved.op_id)
    assert out["operation"]["state"] == "succeeded" and out["operation"]["recover"] is True
    assert out["operation"]["recovery_id"] == moved.registered["recovery_operation_id"]
    row = dict(tagenv.engine.connect().execute(text(
        "SELECT host_id, execution_kind, state, generation FROM tag_companions WHERE id = :c"),
        {"c": ctx.companion_id}).mappings().one())
    assert row == {"host_id": "host-dsk", "execution_kind": "desktop", "state": "recovering",
                   "generation": generation + 1}
    status, _ = ctx.worker.call("GET", "/whoami")
    assert status == 401, "the old computer's worker is revoked"
    status, out = call(get_tags_setup_routes(), "GET", "/api/tags/recoveries/{op_id}",
                       req("admin", path={"op_id": moved.registered["recovery_operation_id"]}))
    assert status == 200 and out["recovery"]["kind"] == "recover_gateway", out
    status, out = call(get_tags_setup_routes(), "GET", "/api/tags/recoveries/{op_id}",
                       req("p1", path={"op_id": moved.registered["recovery_operation_id"]}))
    assert status == 404


# ---------------------------------------------------------------- the host API and its credential scheme


def test_the_host_api_takes_only_a_host_credential(tagenv) -> None:
    from app.api.tag_host import get_tag_host_routes

    principal, cred_id, secret = desktop(tagenv, "p1", "pid1")
    routes = get_tag_host_routes()

    def host_call(method: str, path: str, *, auth: str | None, body: Any = None, query: dict | None = None):
        headers = {"authorization": auth} if auth else {}
        return call(routes, method, "/api/tag-host/v1" + path, req(None, body=body, headers=headers, query=query))

    status, out = host_call("POST", "/hello", auth=None, body={})
    assert status == 401 and out["error"] == "invalid_credential"
    status, out = host_call("POST", "/hello", auth=f"CremindHost {cred_id}.{secret}",
                            body={"name": "Laptop", "platform": "macos", "version": "0.0.19",
                                  "capabilities": {"usb": {"available": True}}, "status": {"state": "running"}})
    assert status == 200 and out["host_id"] == "host-dsk" and out["work_pending"] == 0
    assert host_view("p1", "host-dsk")["name"] == "Laptop"
    status, out = host_call("GET", "/work", auth=f"CremindHost {cred_id}.{secret}", query={"wait": "0"})
    assert status == 200 and out == {"work": []}


# ---------------------------------------------------------------- workers moving in from Cremind Connect


def legacy_worker(env, companion_id: str, profile: str | None, profile_id: str | None, *, mode: str = "private",
                  execution_kind: str = "legacy_external", generation: int = 2) -> None:
    """A connection Cremind Connect set up and runs (what every one made before gateway computers is)."""
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_at, updated_at, mode, owner_profile, "
                       "owner_profile_id, generation, state, execution_kind) VALUES (:i, 'Connect PC', 0, 0, :m, "
                       ":p, :pid, :g, 'active', :k)"),
                  {"i": companion_id, "m": mode, "p": profile, "pid": profile_id, "g": generation,
                   "k": execution_kind})


def test_a_computer_takes_over_the_workers_cremind_connect_ran_there(tagenv) -> None:
    from app.tags import hosts
    from app.tags.service import TagError

    principal, _, _ = desktop(tagenv, "p1", "pid1")
    legacy_worker(tagenv, "c-mine", "p1", "pid1")
    legacy_worker(tagenv, "c-theirs", "p2", "pid2")
    legacy_worker(tagenv, "c-shared", None, None, mode="legacy_shared")
    legacy_worker(tagenv, "c-hosted", "p1", "pid1", execution_kind="server")

    out = run(hosts.adopt_legacy_worker(principal, "c-mine"))
    assert out == {"companion_id": "c-mine", "generation": 2, "host_id": "host-dsk",
                   "profile": {"name": "p1", "id": "pid1"}}
    row = dict(tagenv.engine.connect().execute(text(
        "SELECT host_id, execution_kind, generation FROM tag_companions WHERE id = 'c-mine'")).mappings().one())
    assert row == {"host_id": "host-dsk", "execution_kind": "desktop", "generation": 2}, "moved in as it was"
    assert run(hosts.adopt_legacy_worker(principal, "c-mine")) == out, "a retried request answers the same"

    for companion_id, code in (("c-theirs", "connection_not_found"), ("c-shared", "connection_not_found"),
                               ("c-missing", "connection_not_found"), ("c-hosted", "not_legacy"), ("", "invalid_worker")):
        with pytest.raises(TagError) as info:
            run(hosts.adopt_legacy_worker(principal, companion_id))
        assert info.value.code == code, companion_id
    assert scalar(tagenv, "SELECT execution_kind FROM tag_companions WHERE id = 'c-theirs'") == "legacy_external"

    # The server's own computer serves every profile: any profile's worker moves onto it.
    assert run(hosts.adopt_legacy_worker(server(), "c-theirs"))["host_id"] == SRV_ID
    with pytest.raises(TagError) as info:
        run(hosts.adopt_legacy_worker(server(), "c-mine"))
    assert info.value.code == "not_legacy", "a worker another computer took over stays there"


def test_the_host_api_adopts_and_reports_the_move(tagenv) -> None:
    from app.api.tag_host import get_tag_host_routes

    _, cred_id, secret = desktop(tagenv, "p1", "pid1")
    legacy_worker(tagenv, "c-mine", "p1", "pid1")
    auth = {"authorization": f"CremindHost {cred_id}.{secret}"}
    path = "/api/tag-host/v1/workers/{companion_id}/adopt"
    status, out = call(get_tag_host_routes(), "POST", path,
                       req(None, path={"companion_id": "c-mine"}, body={}, headers=auth))
    assert status == 200 and out["companion_id"] == "c-mine" and out["host_id"] == "host-dsk", out
    status, out = call(get_tag_host_routes(), "POST", path, req(None, path={"companion_id": "c-mine"}, body={}))
    assert status == 401

    from app.tags import hosts

    principal = run(hosts.authenticate_host(cred_id, secret))
    run(hosts.host_hello(principal, {
        "name": "Laptop", "platform": "windows", "version": "0.0.19", "capabilities": {},
        "status": {"state": "running", "gateways": [], "workers": [],
                   "migration": {"moved": 1, "failed": 0, "rolled_back": 1, "junk": 5, "failed_x": "2"}}}))
    assert host_view("p1", "host-dsk")["migration"] == {"moved": 1, "failed": 0, "rolled_back": 1}


def test_the_guard_keeps_host_credentials_on_the_host_api() -> None:
    from app.middleware.tag_connector_guard import TagConnectorGuard

    reached: list[str] = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def go(path: str) -> list[dict]:
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "path": path, "method": "GET",
                 "headers": [(b"authorization", b"CremindHost tagh_abc.secret")]}
        await TagConnectorGuard(app)(scope, receive, send)
        return sent

    sent = asyncio.run(go("/api/tags/connections"))
    assert sent[0]["status"] == 401 and reached == []
    assert asyncio.run(go("/api/tag-host/v1/hello")) == [] and reached == ["/api/tag-host/v1/hello"]
