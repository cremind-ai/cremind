"""Simple device setup (cremind-tag docs/connect-setup.md, docs/setup-api.md):
setup sessions and the bootstrap API, private workers, grants, operations,
the recovery vault, isolation between profiles, and the admin fence.

A fake Cremind Connect (an Ed25519 installation key that signs every bootstrap
request) and fake devices (X25519 identity keys) drive the real handlers.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tests.tags._helpers import body_of, find_handler, rows, run, scalar, tagenv  # noqa: F401

ORIGIN = "https://cremind.test:1180"
_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw


def _pub(key) -> bytes:
    return key.public_key().public_bytes(_RAW, _RAW_PUB)


@pytest.fixture(autouse=True)
def _authority_dir(tmp_path, monkeypatch):
    from app.tags import authority

    monkeypatch.setattr(authority, "directory", lambda: tmp_path / ".tag-authority")
    monkeypatch.setenv("CREMIND_TAGS_SIMPLE_SETUP", "1")
    authority.reset_cache()
    yield
    authority.reset_cache()


def req(username: str | None = "p1", *, path: dict | None = None, body: Any = None,
        headers: dict | None = None, query: dict | None = None):
    raw = json.dumps(body).encode() if body is not None else b""

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    async def _body():
        return raw

    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    if username is not None and "authorization" not in hdrs:
        hdrs["authorization"] = f"Bearer token-of-{username}"
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=username is not None, username=username or ""),
        path_params=path or {}, query_params=query or {}, json=_json, body=_body, headers=hdrs,
    )


def _routes():
    from app.api.tag_connector import get_tag_connector_routes
    from app.api.tag_setup_bootstrap import get_tag_setup_bootstrap_routes
    from app.api.tags_setup import get_tags_setup_routes

    return get_tags_setup_routes(), get_tag_setup_bootstrap_routes(), get_tag_connector_routes()


def call(routes, method: str, path: str, request) -> tuple[int, dict]:
    resp = run(find_handler(routes, path, method)(request))
    return resp.status_code, body_of(resp)


class Connect:
    """A Cremind Connect installation: its key signs every bootstrap request."""

    def __init__(self, computer: str = "DESKTOP-A"):
        self.sk = Ed25519PrivateKey.generate()
        self.pub = _pub(self.sk).hex()
        self.id = hashlib.sha256(bytes.fromhex(self.pub)).digest()[:16].hex()
        self.computer = computer
        self.nonce = ""
        self.token = ""
        self.session = ""

    def installation(self) -> dict[str, str]:
        return {"id": self.id, "public_key": self.pub, "computer": self.computer, "platform": "windows",
                "version": "0.2.0"}

    def sign(self, action: str, body: dict[str, Any]) -> str:
        from app.tags.setup import proof_message

        return self.sk.sign(proof_message(action, self.session, self.nonce, body)).hex()

    def header(self) -> dict[str, str]:
        return {"authorization": f"CremindSetup {self.session}.{self.token}"}

    def post(self, action: str, body: dict[str, Any]) -> tuple[int, dict]:
        _, boot, _ = _routes()
        body = {**body}
        body["proof"] = self.sign(action, body)
        path = "/api/tag-setup/v1/sessions/{session_id}" + ("" if action == "poll" else f"/{action}")
        return call(boot, "POST", path, req(None, path={"session_id": self.session}, body=body,
                                               headers=self.header()))

    def poll(self) -> tuple[int, dict]:
        _, boot, _ = _routes()
        headers = {**self.header(), "x-cremind-connect-proof": self.sign("poll", {})}
        return call(boot, "GET", "/api/tag-setup/v1/sessions/{session_id}",
                    req(None, path={"session_id": self.session}, headers=headers))

    def adopt(self, launch_url: str) -> None:
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(launch_url).query)
        self.session, self.token = q["session"][0], q["token"][0]


class Device:
    def __init__(self, role: str):
        from app.tags import protocol_v2 as v2

        self.role = role
        self.sk = X25519PrivateKey.generate()
        self.ik = _pub(self.sk).hex()
        self.device_id = v2.device_id(role, bytes.fromhex(self.ik)).hex()
        self.short_id = v2.short_id(bytes.fromhex(self.device_id))
        self.challenge = hashlib.sha256(self.ik.encode()).hexdigest()[:32]

    def identify(self, **extra) -> dict[str, Any]:
        return {"device_id": self.device_id, "ik": self.ik, "proto": 2, "role": 1, "owner_state": 0, "gen": 0,
                "authority_id": None, "challenge": self.challenge, "fw": "0.2.0", **extra}


def setup_code(role: str, short_id: int, secret: bytes = bytes(range(10))) -> str:
    """Mirror of cremind_tag.secure.codes (payload -> base32 + GF(32) check)."""
    from app.tags import protocol_v2 as v2

    payload = bytes([0x20 | v2.ROLES[role]]) + short_id.to_bytes(4, "little") + secret
    value = int.from_bytes(payload, "big")
    symbols = "".join(v2.ALPHABET[(value >> (5 * (23 - i))) & 31] for i in range(24))
    return symbols + v2.ALPHABET[v2._check_value(symbols)]


class Worker:
    def __init__(self, hardware: str, content: str, controller: X25519PrivateKey, companion_id: str):
        self.hardware, self.content = hardware, content
        self.controller = controller
        self.controller_pub = _pub(controller).hex()
        self.companion_id = companion_id

    def call(self, method: str, path: str, *, body: Any = None, path_params: dict | None = None,
             kind: str = "hardware") -> tuple[int, dict]:
        _, _, connector = _routes()
        auth = self.hardware if kind == "hardware" else self.content
        return call(connector, method, "/api/tag-connector/v1" + path,
                    req(None, path=path_params, body=body, headers={"authorization": auth}))

    def operation_ids(self) -> list[str]:
        status, out = self.call("GET", "/state")
        assert status == 200, out
        return [o["id"] for o in out["operations"]]


def connect_gateway(env, profile: str = "p1", *, connect: Connect | None = None,
                    gateway: Device | None = None) -> SimpleNamespace:
    """The whole first-gateway flow; returns the worker and its ids."""
    from app.tags import credentials as creds

    prof, _, _ = _routes()
    connect = connect or Connect()
    gateway = gateway or Device("gateway")
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req(profile, body={"operation": "connect_gateway", "server_url": ORIGIN}))
    assert status == 201, out
    session_id = out["session"]["id"]
    assert out["launch_url"].startswith("cremind-connect://setup?v=1&server=https%3A%2F%2Fcremind.test%3A1180")
    connect.adopt(out["launch_url"])
    status, bound = connect.post("bind", {"installation": connect.installation()})
    assert status == 200, bound
    connect.nonce = bound["server_nonce"]
    assert bound["profile"]["name"] == profile
    assert len(bound["verification_phrase"].split()) == 4
    status, out = connect.post("approve", {"gateway": gateway.identify()})
    assert status == 200, out
    status, out = call(prof, "GET", "/api/tags/setup-sessions/{session_id}", req(profile, path={"session_id": session_id}))
    assert out["session"]["state"] == "waiting_for_confirmation"
    assert out["session"]["verification_phrase"] == bound["verification_phrase"]
    status, out = call(prof, "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                       req(profile, path={"session_id": session_id}, body={}))
    assert status == 200 and out["session"]["state"] == "redeeming", out
    controller = X25519PrivateKey.generate()
    hw_secret, ct_secret = creds.new_secret(), creds.new_secret()
    status, redeemed = connect.post("redeem", {
        "idempotency_key": "redeem-key-1", "controller_pub": _pub(controller).hex(),
        "credentials": {"hardware_sha256": creds.hash_secret(hw_secret),
                        "content_sha256": creds.hash_secret(ct_secret)}})
    assert status == 200, redeemed
    worker = Worker(f"CremindTag {redeemed['credentials']['hardware_id']}.{hw_secret}",
                    f"CremindTag {redeemed['credentials']['content_id']}.{ct_secret}",
                    controller, redeemed["companion_id"])
    return SimpleNamespace(session_id=session_id, connect=connect, gateway=gateway, worker=worker,
                           redeemed=redeemed, operation_id=redeemed["operation_id"], bound=bound)


def grant_for(worker: Worker, op_id: str, device: Device, op: str, gen_from: int = 0, **extra) -> tuple[int, dict]:
    body = {"operation_id": op_id, "op": op, "device_id": device.device_id, "role": device.role,
            "gen_from": gen_from, "challenge": device.challenge, **extra}
    return worker.call("POST", "/grants", body=body)


def finish_claim(env, ctx) -> None:
    status, g = grant_for(ctx.worker, ctx.operation_id, ctx.gateway, "claim")
    assert status == 200, g
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": ctx.operation_id},
                                  body={"stage": "claimed", "state": "succeeded", "device": {"gen": 1}})
    assert status == 200, out
    status, out = ctx.worker.call("POST", "/heartbeat", body={"companion": {"version": "0.2.0"}})
    assert status == 200, out


# ---------------------------------------------------------------- protocol helpers


def test_protocol_helpers_match_the_cremind_tag_fixtures() -> None:
    from app.tags import protocol_v2 as v2

    # protocol/fixtures/v2_secure.json (cremind-tag)
    assert setup_code("tag", 439041101) == "4D6KRART000G40R40M30E2097"
    parsed = v2.parse_setup_code("4D6KR-ART00-0G40R-40M30-E2097", role="tag")
    assert (parsed.role, parsed.short_id, parsed.secret) == ("tag", 439041101, bytes(range(10)))
    assert v2.parse_setup_code("CTAG:480G0000Y3RZ5WZMYQVFFY7SN").role == "bridge"
    assert v2.device_id("gateway", bytes.fromhex(
        "c2d056af26ba47878f6f3a98e836ac49e1efd0815af4ea75395610655e6b5263")).hex() == "5563e4923c0f6c54d7387e5f00377bc5"
    assert v2.short_id(bytes.fromhex("5563e4923c0f6c54d7387e5f00377bc5")) == 2464441173
    grant = v2.encode_grant(
        op="claim", device=bytes.fromhex("5563e4923c0f6c54d7387e5f00377bc5"), role="gateway",
        authority_pub=bytes.fromhex("54b5b549dc398582888635b6cbad0c1a33a11df8fb434990ab18fc3943894a0b"),
        owner=bytes(range(0x40, 0x50)),
        controller=bytes.fromhex("0bd6f676e4507dcc508e28b6293385cb40c0393e03a3338f30dd2989720b4d4f"),
        gen_from=0, challenge=bytes(range(0x80, 0x90)))
    assert grant.hex() == (
        "aa0002010102505563e4923c0f6c54d7387e5f00377bc5030104582054b5b549dc398582888635b6cbad0c1a33a11df8"
        "fb434990ab18fc3943894a0b0550404142434445464748494a4b4c4d4e4f0658200bd6f676e4507dcc508e28b6293385"
        "cb40c0393e03a3338f30dd2989720b4d4f070008010950808182838485868788898a8b8c8d8e8f")


@pytest.mark.parametrize("text, code", [
    ("", "setup_code_invalid"), ("4D6KR-ART00-0G40R-40M30-E2098", "setup_code_invalid"),
    ("U" * 25, "setup_code_invalid"),
])
def test_bad_setup_codes(text: str, code: str) -> None:
    from app.tags import protocol_v2 as v2

    with pytest.raises(v2.SetupCodeError) as info:
        v2.parse_setup_code(text)
    assert info.value.code == code
    with pytest.raises(v2.SetupCodeError) as info:
        v2.parse_setup_code("4D6KR-ART00-0G40R-40M30-E2097", role="bridge")
    assert info.value.code == "setup_code_wrong_role"


# ---------------------------------------------------------------- authority and vault


def test_authority_is_created_once_and_refuses_to_rekey_over_dependents(tagenv, tmp_path) -> None:
    from app.tags import authority
    from app.tags.vault import VaultError, open_record, seal, vault_context

    first = run(authority.get_authority())
    authority.reset_cache()
    again = run(authority.get_authority())
    assert again.authority_pub == first.authority_pub and again.master_kid == first.master_kid
    record = seal(first, {"root": "ab" * 32}, vault_context("pid1", "worker", 1, 1))
    assert open_record(first, record, vault_context("pid1", "worker", 1, 1)) == {"root": "ab" * 32}
    with pytest.raises(VaultError):
        open_record(first, record, vault_context("pid2", "worker", 1, 1))  # another profile's context
    assert "ab" * 32 not in json.dumps(record)

    # Keys gone but nothing depends on them yet: a fresh authority.
    for path in (tmp_path / ".tag-authority").iterdir():
        path.unlink()
    authority.reset_cache()
    fresh = run(authority.get_authority())
    assert fresh.authority_pub != first.authority_pub

    # Keys gone while a binding depends on them: unavailable, never re-keyed.
    ctx = connect_gateway(tagenv)
    assert ctx.redeemed["server"]["authority_pub"] == fresh.authority_pub.hex()
    for path in (tmp_path / ".tag-authority").iterdir():
        path.unlink()
    authority.reset_cache()
    with pytest.raises(authority.AuthorityUnavailable) as info:
        run(authority.get_authority())
    assert info.value.code == "authority_unavailable"
    status, out = grant_for(ctx.worker, ctx.operation_id, ctx.gateway, "claim")
    assert status == 503 and out["error"] == "authority_unavailable"


# ---------------------------------------------------------------- the first gateway


def test_connect_gateway_end_to_end(tagenv) -> None:
    from app.tags import protocol_v2 as v2

    ctx = connect_gateway(tagenv)
    w = ctx.worker
    status, who = w.call("GET", "/whoami")
    assert status == 200 and who["api_version"] == 2 and who["mode"] == "private"
    assert who["worker"]["state"] == "active"
    status, lease = w.call("POST", "/lease", body={})
    assert status == 200 and lease["ttl_s"] == 60 and lease["renew_s"] == 20
    assert ctx.operation_id in w.operation_ids()
    # The claim runs as a run_operation command on the ordinary queue.
    status, cmds = w.call("GET", "/commands", body=None)
    assert [c["kind"] for c in cmds["commands"]] == ["run_operation"]
    assert cmds["commands"][0]["args"] == {"operation_id": ctx.operation_id, "kind": "claim_gateway"}
    status, op = w.call("GET", "/operations/{operation_id}", path_params={"operation_id": ctx.operation_id})
    assert op["operation"]["args"]["device_id"] == ctx.gateway.device_id and op["operation"]["setup_secret"] is None

    status, g = grant_for(w, ctx.operation_id, ctx.gateway, "claim")
    assert status == 200, g
    grant, sig = bytes.fromhex(g["grant"]), bytes.fromhex(g["sig"])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(g["authority_pub"])).verify(sig, v2.GRANT_LABEL + grant)
    assert grant == v2.encode_grant(op="claim", device=bytes.fromhex(ctx.gateway.device_id), role="gateway",
                                    authority_pub=bytes.fromhex(g["authority_pub"]), owner=v2.owner_bytes("pid1"),
                                    controller=bytes.fromhex(w.controller_pub), gen_from=0,
                                    challenge=bytes.fromhex(ctx.gateway.challenge))
    finish_claim_progress = w.call("POST", "/operations/{operation_id}/progress",
                                   path_params={"operation_id": ctx.operation_id},
                                   body={"stage": "claimed", "state": "succeeded", "device": {"gen": 1}})
    assert finish_claim_progress[0] == 200
    assert scalar(tagenv, "SELECT state FROM tag_bindings WHERE device_id = :d", d=ctx.gateway.device_id) == "paired"
    assert scalar(tagenv, "SELECT generation FROM tag_bindings WHERE device_id = :d", d=ctx.gateway.device_id) == 1
    status, out = w.call("POST", "/heartbeat", body={"companion": {"version": "0.2.0"}})
    assert status == 200
    prof, _, _ = _routes()
    status, out = call(prof, "GET", "/api/tags/setup-sessions/{session_id}", req("p1", path={"session_id": ctx.session_id}))
    assert out["session"]["state"] == "completed"
    status, view = call(prof, "GET", "/api/tags/connections", req("p1"))
    assert status == 200 and view["simple_setup"] is True
    [conn] = view["connections"]
    assert conn["status"] == "connected" and conn["computer"]["name"] == "DESKTOP-A"
    assert conn["gateway"]["state"] == "ready" and conn["gateway"]["device_id"] == ctx.gateway.device_id


def test_bootstrap_rules(tagenv) -> None:
    prof, boot, _ = _routes()
    connect = Connect()
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "connect_gateway", "server_url": ORIGIN + "/path"}))
    assert status == 422 and out["error"] == "invalid_server_url"
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "connect_gateway", "server_url": ORIGIN}))
    connect.adopt(out["launch_url"])
    sid = out["session"]["id"]
    # A wrong token, a JWT or a connector credential never reach the session.
    for header in ("CremindSetup " + sid + ".wrong-token-wrong-token-wrong-token-12", "Bearer x",
                   "CremindTag tagc_" + "a" * 26 + "." + "b" * 43):
        status, out = call(boot, "POST", "/api/tag-setup/v1/sessions/{session_id}/bind",
                           req(None, path={"session_id": sid}, body={"installation": connect.installation()},
                               headers={"authorization": header}))
        assert status == 401, (header, out)
    # A proof by another key is refused.
    other = Connect()
    other.session, other.token = connect.session, connect.token
    body = {"installation": connect.installation()}
    body["proof"] = other.sign("bind", body)
    status, out = call(boot, "POST", "/api/tag-setup/v1/sessions/{session_id}/bind",
                       req(None, path={"session_id": sid}, body=body, headers=connect.header()))
    assert status == 401 and out["error"] == "invalid_proof"
    status, bound = connect.post("bind", {"installation": connect.installation()})
    assert status == 200
    connect.nonce = bound["server_nonce"]
    # A second Connect cannot take the session over.
    other.nonce = ""
    status, out = other.post("bind", {"installation": other.installation()})
    assert status == 409 and out["error"] == "already_bound"
    # Binding again from the same installation is idempotent.
    connect_nonce = connect.nonce
    connect.nonce = ""
    status, again = connect.post("bind", {"installation": connect.installation()})
    connect.nonce = connect_nonce
    assert status == 200 and again["verification_phrase"] == bound["verification_phrase"]
    # v1 firmware and inconsistent identities are refused.
    gw = Device("gateway")
    status, out = connect.post("approve", {"gateway": gw.identify(proto=1)})
    assert status == 422 and out["error"] == "v1_firmware"
    status, out = connect.post("approve", {"gateway": {**gw.identify(), "device_id": "00" * 16}})
    assert status == 422 and out["error"] == "invalid_gateway"
    # Redeem before the browser confirmed.
    status, out = connect.post("approve", {"gateway": gw.identify()})
    assert status == 200
    status, out = connect.post("redeem", {"idempotency_key": "k" * 10, "controller_pub": "11" * 32,
                                          "credentials": {"hardware_sha256": "22" * 32, "content_sha256": "33" * 32}})
    assert status == 409 and out["error"] == "not_confirmed"
    # The confirmation must come from the sign-in that started the session.
    status, out = call(prof, "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                       req("p1", path={"session_id": sid}, body={}, headers={"authorization": "Bearer other"}))
    assert status == 403 and out["error"] == "session_mismatch"
    status, out = connect.poll()
    assert status == 200 and out["state"] == "waiting_for_confirmation" and out["native_approved"]


def test_sessions_expire_and_cancel(tagenv, monkeypatch) -> None:
    import app.tags.setup as setup_mod

    prof, _, _ = _routes()
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "connect_gateway", "server_url": ORIGIN}))
    sid = out["session"]["id"]
    connect = Connect()
    connect.adopt(out["launch_url"])
    real = setup_mod.now_ms
    monkeypatch.setattr(setup_mod, "now_ms", lambda: real() + setup_mod.SESSION_TTL_MS + 1000)
    status, out = connect.post("bind", {"installation": connect.installation()})
    assert status == 410 and out["error"] == "session_expired"
    status, out = call(prof, "GET", "/api/tags/setup-sessions/{session_id}", req("p1", path={"session_id": sid}))
    assert out["session"]["state"] == "expired"
    monkeypatch.setattr(setup_mod, "now_ms", real)
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "connect_gateway", "server_url": ORIGIN}))
    sid2 = out["session"]["id"]
    status, out = call(prof, "DELETE", "/api/tags/setup-sessions/{session_id}", req("p1", path={"session_id": sid2}))
    assert out["session"]["state"] == "cancelled"


def test_idempotency_key_replays_the_first_answer(tagenv) -> None:
    prof, _, _ = _routes()
    body = {"operation": "connect_gateway", "server_url": ORIGIN}
    headers = {"idempotency-key": "same-key-123"}
    s1, a = call(prof, "POST", "/api/tags/setup-sessions", req("p1", body=body, headers=headers))
    s2, b = call(prof, "POST", "/api/tags/setup-sessions", req("p1", body=body, headers=headers))
    assert s1 == s2 == 201 and a == b
    s3, c = call(prof, "POST", "/api/tags/setup-sessions",
                 req("p1", body={**body, "operation": "probe"}, headers=headers))
    assert s3 == 409 and c["error"] == "idempotency_key_reused"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_setup_sessions") == 1


def test_probe_completes_on_bind(tagenv) -> None:
    prof, _, _ = _routes()
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "probe", "server_url": ORIGIN}))
    connect = Connect("LAPTOP-B")
    connect.adopt(out["launch_url"])
    status, bound = connect.post("bind", {"installation": connect.installation()})
    assert bound["session"]["state"] == "completed"
    status, out = call(prof, "GET", "/api/tags/setup-sessions/{session_id}",
                       req("p1", path={"session_id": out["session"]["id"]}))
    assert out["session"]["computer"]["name"] == "LAPTOP-B"


# ---------------------------------------------------------------- isolation


def test_other_profiles_and_the_admin_never_see_private_hardware(tagenv) -> None:
    from app.api.tags import get_tags_routes
    from app.api.tags_hardware import get_tags_hardware_routes

    ctx = connect_gateway(tagenv, "p1")
    finish_claim(tagenv, ctx)
    prof, _, _ = _routes()
    status, view = call(prof, "GET", "/api/tags/connections", req("p2"))
    assert view["connections"] == [] and view["active"]["operations"] == []
    status, out = call(prof, "GET", "/api/tags/setup-sessions/{session_id}", req("p2", path={"session_id": ctx.session_id}))
    assert status == 404
    device_row = scalar(tagenv, "SELECT tag_device_id FROM tag_bindings WHERE device_id = :d", d=ctx.gateway.device_id)
    for route in ("unpair", "pause", "resume", "test"):
        status, out = call(prof, "POST", f"/api/tags/devices/{{device_id}}/{route}",
                           req("p2", path={"device_id": device_row}, body={}))
        assert status == 404, (route, out)
    status, out = call(prof, "POST", "/api/tags/recoveries",
                       req("p2", body={"companion_id": ctx.worker.companion_id, "server_url": ORIGIN}))
    assert status == 404
    # Companion listing and manual credentials.
    tags_routes = get_tags_routes()
    resp = run(find_handler(tags_routes, "/api/tags/companions", "GET")(req("p2")))
    assert ctx.worker.companion_id not in json.dumps(body_of(resp))
    resp = run(find_handler(tags_routes, "/api/tags/credentials", "POST")(
        req("p2", body={"companion_id": ctx.worker.companion_id})))
    assert resp.status_code == 404
    resp = run(find_handler(tags_routes, "/api/tags/credentials", "POST")(
        req("p1", body={"companion_id": ctx.worker.companion_id})))
    assert resp.status_code == 409 and body_of(resp)["error"] == "managed_by_connect"
    # The admin's shared-hardware API has no route around ownership.
    hw = get_tags_hardware_routes()
    inventory = body_of(run(find_handler(hw, "/api/tags/hardware", "GET")(req("admin"))))
    assert ctx.worker.companion_id not in json.dumps(inventory)
    for path, method, params, body in (
        ("/api/tags/hardware/companions/{companion_id}/rotate", "POST", {"companion_id": ctx.worker.companion_id}, {}),
        ("/api/tags/hardware/companions/{companion_id}", "DELETE", {"companion_id": ctx.worker.companion_id}, None),
        ("/api/tags/hardware/devices/{device_id}", "PATCH", {"device_id": device_row}, {"name": "x"}),
        ("/api/tags/hardware/devices/{device_id}", "DELETE", {"device_id": device_row}, None),
        ("/api/tags/hardware/tags/{device_id}/release", "POST", {"device_id": device_row}, {}),
    ):
        resp = run(find_handler(hw, path, method)(req("admin", path=params, body=body)))
        assert resp.status_code == 404, (path, body_of(resp))
    resp = run(find_handler(hw, "/api/tags/hardware/commands", "POST")(
        req("admin", body={"companion_id": ctx.worker.companion_id, "kind": "collect_diagnostics"})))
    assert resp.status_code == 404


def test_a_worker_cannot_touch_another_workers_operations_or_devices(tagenv) -> None:
    a = connect_gateway(tagenv, "p1")
    b = connect_gateway(tagenv, "p2")
    status, out = a.worker.call("GET", "/operations/{operation_id}", path_params={"operation_id": b.operation_id})
    assert status == 404
    status, out = grant_for(a.worker, b.operation_id, b.gateway, "claim")
    assert status == 403
    status, out = grant_for(a.worker, a.operation_id, b.gateway, "claim")
    assert status == 403
    status, out = a.worker.call("PUT", "/vault/{subject}", path_params={"subject": b.gateway.device_id},
                                body={"expected_version": None, "stage": "committed", "generation": 0, "state": {}})
    assert status == 403
    # The same physical gateway cannot be connected twice.
    prof, _, _ = _routes()
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p2", body={"operation": "connect_gateway", "server_url": ORIGIN}))
    c = Connect()
    c.adopt(out["launch_url"])
    status, bound = c.post("bind", {"installation": c.installation()})
    c.nonce = bound["server_nonce"]
    status, out = c.post("approve", {"gateway": a.gateway.identify()})
    assert status == 409 and out["error"] == "device_owned"


def test_profile_deletion_revokes_its_workers_and_keeps_tombstones(tagenv) -> None:
    from app.tags.ownership import revoke_profile_workers

    ctx = connect_gateway(tagenv, "p2")
    finish_claim(tagenv, ctx)

    async def _revoke():
        async with tagenv.store.engine.begin() as conn:
            return await revoke_profile_workers(conn, "p2")

    assert run(_revoke()) == [ctx.worker.companion_id]
    status, out = ctx.worker.call("GET", "/whoami")
    assert status == 401
    assert scalar(tagenv, "SELECT highest_generation FROM tag_revocations WHERE device_id = :d",
                  d=ctx.gateway.device_id) >= 1
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_bindings") == 0


# ---------------------------------------------------------------- bridges and tags


def _pair(env, ctx, role: str, device: Device, *, candidate: dict | None = None,
          name: str = "Kitchen") -> tuple[str, str]:
    """Discovery -> worker report -> pairing -> grant -> progress. Returns
    (pairing id, device row id)."""
    prof, _, _ = _routes()
    status, disc = call(prof, "POST", "/api/tags/discovery",
                        req("p1", body={"role": role, "setup_code": setup_code(role, device.short_id)}))
    assert status == 201, disc
    report = candidate or ({"uuid": device.device_id, "rssi": -50} if role == "bridge"
                           else {"tag_id": device.short_id, "bridge_hw_id": "br-" + ctx.bridge.device_id, "rssi": -60})
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": disc["discovery"]["id"]},
                                  body={"candidates": [report], "state": "succeeded"})
    assert status == 200, out
    status, disc = call(prof, "GET", "/api/tags/discovery/{op_id}", req("p1", path={"op_id": disc["discovery"]["id"]}))
    assert disc["discovery"]["state"] == "found", disc
    cand = disc["discovery"]["candidates"][0]
    assert disc["discovery"]["recommended"] == cand["id"]
    status, pairing = call(prof, "POST", "/api/tags/pairings",
                           req("p1", body={"discovery_id": disc["discovery"]["id"], "candidate_id": cand["id"],
                                           "name": name}))
    assert status == 201, pairing
    op_id = pairing["pairing"]["id"]
    status, op = ctx.worker.call("GET", "/operations/{operation_id}", path_params={"operation_id": op_id})
    assert op["operation"]["setup_secret"] == bytes(range(10)).hex()
    status, g = grant_for(ctx.worker, op_id, device, "pair", ik=device.ik)
    assert status == 200, g
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": op_id},
                                  body={"stage": "paired", "device": {"gen": 1, "fw": "0.2.0", "max_tags": 20,
                                                                      "fontpack_ok": True, "epoch": 1}})
    assert status == 200, out
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress",
                                  path_params={"operation_id": op_id}, body={"stage": "done", "state": "succeeded"})
    assert status == 200, out
    row = scalar(env, "SELECT tag_device_id FROM tag_bindings WHERE device_id = :d", d=device.device_id)
    return op_id, row


def test_pair_a_bridge_and_a_tag_then_content_flows(tagenv) -> None:
    from app.tags import service

    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    prof, _, _ = _routes()
    # A tag needs a ready bridge first.
    tag = Device("tag")
    status, out = call(prof, "POST", "/api/tags/discovery",
                       req("p1", body={"role": "tag", "setup_code": setup_code("tag", tag.short_id)}))
    assert status == 409 and out["error"] == "no_ready_bridge"
    ctx.bridge = Device("bridge")
    _, bridge_row = _pair(tagenv, ctx, "bridge", ctx.bridge)
    assert scalar(tagenv, "SELECT state FROM tag_bindings WHERE device_id = :d", d=ctx.bridge.device_id) == "ready"
    # The pairing grant refuses a device that is not the one on the label.
    op_id, tag_row = _pair(tagenv, ctx, "tag", tag)
    assert scalar(tagenv, "SELECT clear_required FROM tag_devices WHERE id = :i", i=tag_row) in (0, False)
    assert scalar(tagenv, "SELECT bridge_device_id FROM tag_devices WHERE id = :i", i=tag_row) == bridge_row
    status, pairing = call(prof, "GET", "/api/tags/pairings/{op_id}", req("p1", path={"op_id": op_id}))
    assert pairing["pairing"]["state"] == "succeeded" and pairing["pairing"]["first_tag"] is True
    assert pairing["pairing"]["device"]["state"] == "ready"
    # Content flows to the new tag through the content credential.
    delivery = run(service.display("p1", tag_row, {"title": "Hello"}))
    status, synced = ctx.worker.call("POST", "/sync", body={"cursor": None}, kind="content")
    assert status == 200, synced
    assert [j["delivery_id"] for j in synced["outstanding"]] == [delivery["id"]]
    assert synced["tags"][0]["tag_id"] == f"{tag.short_id:08X}"
    # Send test uses the same path.
    status, out = call(prof, "POST", "/api/tags/devices/{device_id}/test", req("p1", path={"device_id": tag_row}, body={}))
    assert status == 201, out
    # Pause holds content; the connection view shows it.
    status, out = call(prof, "POST", "/api/tags/devices/{device_id}/pause", req("p1", path={"device_id": tag_row}, body={}))
    assert status == 200 and out["device"]["paused"] is True
    status, view = call(prof, "GET", "/api/tags/connections", req("p1"))
    [conn] = view["connections"]
    assert [b["state"] for b in conn["bridges"]] == ["ready"] and conn["bridges"][0]["capacity"]["assigned"] == 1
    assert conn["tags"][0]["paused"] is True


def test_pair_grant_refuses_a_device_that_is_not_on_the_label(tagenv) -> None:
    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    prof, _, _ = _routes()
    bridge = Device("bridge")
    status, disc = call(prof, "POST", "/api/tags/discovery",
                        req("p1", body={"role": "bridge", "setup_code": setup_code("bridge", bridge.short_id)}))
    ctx.worker.call("POST", "/operations/{operation_id}/progress",
                    path_params={"operation_id": disc["discovery"]["id"]},
                    body={"candidates": [{"uuid": bridge.device_id, "rssi": -40}], "state": "succeeded"})
    status, disc = call(prof, "GET", "/api/tags/discovery/{op_id}", req("p1", path={"op_id": disc["discovery"]["id"]}))
    status, pairing = call(prof, "POST", "/api/tags/pairings", req("p1", body={
        "discovery_id": disc["discovery"]["id"], "candidate_id": disc["discovery"]["candidates"][0]["id"]}))
    op_id = pairing["pairing"]["id"]
    impostor = Device("bridge")
    status, out = grant_for(ctx.worker, op_id, impostor, "pair", ik=impostor.ik)
    assert status == 403 and out["error"] == "grant_refused"
    status, out = grant_for(ctx.worker, op_id, bridge, "pair", ik=impostor.ik)
    assert status == 422
    # A discovery report whose uuid does not carry the label's short id is ignored.
    status, disc2 = call(prof, "POST", "/api/tags/discovery",
                         req("p1", body={"role": "bridge", "setup_code": setup_code("bridge", Device("bridge").short_id)}))
    ctx.worker.call("POST", "/operations/{operation_id}/progress", path_params={"operation_id": disc2["discovery"]["id"]},
                    body={"candidates": [{"uuid": bridge.device_id, "rssi": -40}], "state": "succeeded"})
    status, view = call(prof, "GET", "/api/tags/discovery/{op_id}", req("p1", path={"op_id": disc2["discovery"]["id"]}))
    assert view["discovery"]["state"] == "not_found" and view["discovery"]["candidates"] == []


def test_remove_a_tag_and_a_bridge(tagenv) -> None:
    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    ctx.bridge = Device("bridge")
    _, bridge_row = _pair(tagenv, ctx, "bridge", ctx.bridge)
    tag = Device("tag")
    _, tag_row = _pair(tagenv, ctx, "tag", tag)
    prof, _, _ = _routes()
    status, out = call(prof, "POST", "/api/tags/devices/{device_id}/unpair", req("p1", path={"device_id": bridge_row}, body={}))
    assert status == 200 and out["device"]["state"] == "removal_pending"
    assert out["device"]["affected_tag_ids"] == [tag_row]
    assert scalar(tagenv, "SELECT status FROM tag_devices WHERE id = :i", i=tag_row) == "needs_bridge"
    op_id = out["operation"]["id"]
    status, g = grant_for(ctx.worker, op_id, ctx.bridge, "release", gen_from=1)
    assert status == 200, g
    status, out = ctx.worker.call("POST", "/operations/{operation_id}/progress", path_params={"operation_id": op_id},
                                  body={"state": "succeeded"})
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_bindings WHERE device_id = :d", d=ctx.bridge.device_id) == 0
    assert scalar(tagenv, "SELECT cleanup FROM tag_revocations WHERE device_id = :d", d=ctx.bridge.device_id) == "done"
    # A removed tag's content is revoked at once (before any physical cleanup).
    status, out = call(prof, "POST", "/api/tags/devices/{device_id}/unpair", req("p1", path={"device_id": tag_row}, body={}))
    assert status == 200 and scalar(tagenv, "SELECT clear_required FROM tag_devices WHERE id = :i", i=tag_row) in (1, True)
    status, synced = ctx.worker.call("POST", "/sync", body={"cursor": None}, kind="content")
    assert synced["outstanding"] == []
    # Force: nothing physical will finish; the tombstone says abandoned.
    status, out = call(prof, "POST", "/api/tags/devices/{device_id}/unpair",
                       req("p1", path={"device_id": tag_row}, body={"force": True}))
    assert status == 200
    assert scalar(tagenv, "SELECT cleanup FROM tag_revocations WHERE device_id = :d", d=tag.device_id) == "abandoned"


# ---------------------------------------------------------------- recovery and the vault


def test_recover_on_a_replacement_computer(tagenv) -> None:
    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    old = ctx.worker
    status, out = old.call("PUT", "/vault/{subject}", path_params={"subject": "worker"},
                           body={"expected_version": None, "stage": "committed", "generation": 0,
                                 "state": {"controller_priv": "aa" * 32}})
    assert status == 200 and out["version"] == 1
    status, out = old.call("PUT", "/vault/{subject}", path_params={"subject": "worker"},
                           body={"expected_version": None, "stage": "committed", "generation": 0, "state": {}})
    assert status == 409 and out["error"] == "version_conflict" and out["version"] == 1
    status, out = old.call("GET", "/vault")
    assert status == 403 and out["error"] == "no_recovery"
    assert "aa" * 32 not in json.dumps(rows(tagenv, "SELECT * FROM tag_vault"))

    prof, _, _ = _routes()
    status, started = call(prof, "POST", "/api/tags/recoveries",
                           req("p1", body={"companion_id": old.companion_id, "server_url": ORIGIN}))
    assert status == 201, started
    recovery_id = started["recovery"]["id"]
    connect = Connect("NEW-PC")
    connect.adopt(started["launch_url"])
    status, bound = connect.post("bind", {"installation": connect.installation()})
    assert bound["recover"]["gateway_device_id"] == ctx.gateway.device_id
    connect.nonce = bound["server_nonce"]
    authority_id = bound["server"]["authority_id"]
    status, out = connect.post("approve", {"gateway": Device("gateway").identify(owner_state=1, authority_id=authority_id)})
    assert status == 409 and out["error"] == "wrong_gateway"
    status, out = connect.post("approve", {"gateway": ctx.gateway.identify(owner_state=1, gen=1,
                                                                          authority_id=authority_id)})
    assert status == 200, out
    status, out = call(prof, "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                       req("p1", path={"session_id": started["session"]["id"]}, body={}))
    assert status == 200
    from app.tags import credentials as creds

    controller = X25519PrivateKey.generate()
    hw, ct = creds.new_secret(), creds.new_secret()
    status, redeemed = connect.post("redeem", {
        "idempotency_key": "recover-1", "controller_pub": _pub(controller).hex(),
        "credentials": {"hardware_sha256": creds.hash_secret(hw), "content_sha256": creds.hash_secret(ct)}})
    assert status == 200 and redeemed["companion_id"] == old.companion_id
    assert redeemed["operation_id"] == recovery_id
    # The old computer is out at once.
    assert old.call("GET", "/whoami")[0] == 401
    new = Worker(f"CremindTag {redeemed['credentials']['hardware_id']}.{hw}",
                 f"CremindTag {redeemed['credentials']['content_id']}.{ct}", controller, old.companion_id)
    status, who = new.call("GET", "/whoami")
    assert who["worker"]["state"] == "recovering" and who["worker"]["generation"] == 1
    status, vault = new.call("GET", "/vault")
    assert status == 200 and vault["entries"][0]["state"] == {"controller_priv": "aa" * 32}
    # RECOVER grant for the gateway at its current generation, naming the NEW controller.
    status, g = grant_for(new, recovery_id, ctx.gateway, "recover", gen_from=1)
    assert status == 200, g
    status, out = new.call("POST", "/operations/{operation_id}/progress", path_params={"operation_id": recovery_id},
                           body={"devices": [{"device_id": ctx.gateway.device_id, "state": "rekeyed", "gen": 2}],
                                 "state": "succeeded"})
    assert status == 200, out
    status, rec = call(prof, "GET", "/api/tags/recoveries/{op_id}", req("p1", path={"op_id": recovery_id}))
    assert rec["recovery"]["state"] == "succeeded"
    assert rec["recovery"]["devices"][0]["state"] == "rekeyed"
    status, who = new.call("GET", "/whoami")
    assert who["worker"]["state"] == "active"


# ---------------------------------------------------------------- guard


def test_setup_credentials_are_refused_outside_the_setup_api() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.middleware.tag_connector_guard import TagConnectorGuard

    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/tags", ok), Route("/api/tag-setup/v1/sessions/x", ok),
                            Route("/api/tag-connector/v1/whoami", ok)])
    client = TestClient(TagConnectorGuard(app))
    setup_header = {"Authorization": "CremindSetup abc.def"}
    assert client.get("/api/tags", headers=setup_header).status_code == 401
    assert client.get("/api/tag-connector/v1/whoami", headers=setup_header).status_code == 401
    assert client.get("/api/tag-setup/v1/sessions/x", headers=setup_header).status_code == 200
    assert client.get("/api/tag-setup/v1/sessions/x", headers={"Authorization": "CremindTag a.b"}).status_code == 401


# ---------------------------------------------------------------- restore


def test_restore_keeps_revocations_and_generations_and_cancels_setup_work(tagenv) -> None:
    from sqlalchemy import text

    from app.backup.engine import _capture_tag_setup, _close_out_tag_setup

    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    ctx.bridge = Device("bridge")
    _, bridge_row = _pair(tagenv, ctx, "bridge", ctx.bridge)
    removed = Device("tag")
    _, removed_row = _pair(tagenv, ctx, "tag", removed)
    prof, _, _ = _routes()
    call(prof, "POST", "/api/tags/devices/{device_id}/unpair",
         req("p1", path={"device_id": removed_row}, body={"force": True}))
    live = _capture_tag_setup(tagenv.engine)
    assert removed.device_id in {r["device_id"] for r in live["revocations"]}
    # "Restore" an archive from before the revocation, with rewound generations
    # and a setup still in flight.
    with tagenv.engine.begin() as c:
        c.execute(text("DELETE FROM tag_revocations"))
        c.execute(text("UPDATE tag_bindings SET generation = 0"))
        c.execute(text(
            "INSERT INTO tag_bindings (id, device_id, role, identity_pub, short_id, companion_id, owner_profile, "
            "owner_profile_id, state, generation, paused, created_at, updated_at) VALUES "
            "('old', :d, 'tag', :ik, 1, :c, 'p1', 'pid1', 'ready', 1, :paused, 0, 0)"),
            {"d": removed.device_id, "ik": removed.ik, "c": ctx.worker.companion_id, "paused": False})
    status, out = call(prof, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "connect_gateway", "server_url": ORIGIN}))
    report = _close_out_tag_setup(tagenv.engine, live)
    assert report["dropped"] == 1 and report["reconciling"] >= 2
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_bindings WHERE device_id = :d", d=removed.device_id) == 0
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_revocations WHERE device_id = :d", d=removed.device_id) == 1
    assert scalar(tagenv, "SELECT generation FROM tag_bindings WHERE device_id = :d", d=ctx.gateway.device_id) == 1
    assert scalar(tagenv, "SELECT state FROM tag_bindings WHERE device_id = :d", d=ctx.bridge.device_id) == "reconciling"
    assert scalar(tagenv, "SELECT state FROM tag_setup_sessions WHERE id = :i", i=out["session"]["id"]) == "cancelled"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_operations WHERE state IN ('queued','running')") == 0
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_companions WHERE lease_expires_at IS NOT NULL") == 0


def test_heartbeat_with_live_generations_ends_reconciling(tagenv) -> None:
    from sqlalchemy import text

    ctx = connect_gateway(tagenv)
    finish_claim(tagenv, ctx)
    ctx.bridge = Device("bridge")
    _pair(tagenv, ctx, "bridge", ctx.bridge)
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_bindings SET state = 'reconciling' WHERE device_id = :d"), {"d": ctx.bridge.device_id})
    hw = "br-" + ctx.bridge.device_id
    ctx.worker.call("POST", "/heartbeat", body={"devices": [{"kind": "bridge", "hw_id": hw, "gen": 0}]})
    assert scalar(tagenv, "SELECT state FROM tag_bindings WHERE device_id = :d", d=ctx.bridge.device_id) == "reconciling"
    ctx.worker.call("POST", "/heartbeat", body={"devices": [{"kind": "bridge", "hw_id": hw, "gen": 1}]})
    assert scalar(tagenv, "SELECT state FROM tag_bindings WHERE device_id = :d", d=ctx.bridge.device_id) == "ready"
