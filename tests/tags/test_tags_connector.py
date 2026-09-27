"""The connector API (``/api/tag-connector/v1``) and its credential boundary.

Driven through a Starlette app carrying the server's own guard, auth and A2A
middleware in the server's order (``test_tags_api.py`` pins that order in
``app/server.py``), so the scheme checks are the real ones:

- a valid credential works; a wrong secret, an unknown id, a malformed value,
  a missing header and a session JWT (``Bearer``) are 401; a revoked one is 401
  ``credential_revoked``;
- ``CremindTag`` on any other route is 401 from ``TagConnectorGuard``;
- content and hardware credentials cannot reach each other's endpoints (403);
- the profile always comes from the credential, and a content credential
  sees only its profile's deliveries on its companion;
- ``events`` answers 410 past the head (a restore) or behind the pruned
  history; receipts are idempotent and monotonic; previews are bounded PNGs;
- the command long-poll returns as soon as a command is queued.
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.tags import journal  # noqa: E402
from app.tags.journal import JournalEntry  # noqa: E402
from app.tags.projection import TagProjectionWorker  # noqa: E402
from tests.tags._helpers import (  # noqa: E402
    claim, content_credential, enable, find_handler, hardware, make_request, run, scalar,
)

_SECRET = "test-secret-that-is-long-enough-for-hs256"
V1 = "/api/tag-connector/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def client(tagenv, monkeypatch):
    import app.config.settings as settings_mod
    from app.api.tag_connector import get_tag_connector_routes
    from app.api.tags import get_tags_routes
    from app.middleware import A2AAuthGuard, ClientProtocolGuard, TagConnectorGuard
    from app.server import JWTAuthBackend

    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: _SECRET))

    async def generic(request):
        return JSONResponse({"user": getattr(request.user, "username", None),
                             "authenticated": request.user.is_authenticated})

    app = Starlette(
        routes=get_tag_connector_routes() + get_tags_routes() + [Route("/api/generic", generic)],
        middleware=[
            Middleware(ClientProtocolGuard),
            Middleware(TagConnectorGuard),
            Middleware(AuthenticationMiddleware,
                       backend=JWTAuthBackend(secret_provider=settings_mod.BaseConfig.get_jwt_secret)),
            Middleware(A2AAuthGuard),
        ],
    )
    with TestClient(app) as c:
        yield c


def _auth(value: str) -> dict:
    return {"Authorization": value}


def _append(env, profile, n=1):
    async def go():
        async with env.provider.async_engine().begin() as conn:
            await journal.append_async(conn, profile, [
                JournalEntry(kind="notification", payload={"kind": "x", "title": f"n{i}", "preview": "",
                                                           "priority": "normal"}, source_type="t")
                for i in range(n)
            ])
    run(go())
    run(TagProjectionWorker(env.store).project_profile(profile))


@pytest.fixture
def setup(tagenv):
    hw = hardware(tagenv, tags=("T1", "T2"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    claim(tagenv, hw["tags"]["T2"], "p2")
    enable(tagenv, "p1")
    enable(tagenv, "p2")
    return {
        **hw,
        "c1": content_credential(tagenv, "p1", hw["companion_id"]),
        "c2": content_credential(tagenv, "p2", hw["companion_id"]),
    }


# ── credentials ─────────────────────────────────────────────────────────────


def test_whoami_for_each_kind(client, setup) -> None:
    r = client.get(f"{V1}/whoami", headers=_auth(setup["auth"]))
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "hardware" and body["profile"] is None
    assert body["companion_id"] == setup["companion_id"] and body["api_version"] == 1
    r = client.get(f"{V1}/whoami", headers=_auth(setup["c1"]))
    assert r.json()["kind"] == "content" and r.json()["profile"] == "p1"


def test_bad_credentials_are_401(client, setup, tagenv) -> None:
    cred_id, secret = setup["auth"].split(" ", 1)[1].split(".", 1)
    wrong = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    cases = {
        "wrong secret": f"CremindTag {cred_id}.{wrong}",
        "unknown id": f"CremindTag tagc_{'a' * 26}.{secret}",
        "malformed": "CremindTag nonsense",
        "no header": None,
    }
    for label, value in cases.items():
        r = client.get(f"{V1}/whoami", headers=_auth(value) if value else {})
        assert r.status_code == 401, label
        assert r.json()["error"] in ("invalid_credential", "missing_credential"), label
        assert r.json()["detail"] == r.json()["message"]


def test_a_session_jwt_is_refused_by_the_connector(client, setup) -> None:
    from app.config.settings import BaseConfig

    token, _ = BaseConfig.mint_token("p1")
    r = client.get(f"{V1}/whoami", headers=_auth(f"Bearer {token}"))
    assert r.status_code == 401 and r.json()["error"] == "bearer_not_accepted"
    # The same token is fine on a profile route.
    assert client.get("/api/tags", headers=_auth(f"Bearer {token}")).status_code == 200


def test_the_tag_scheme_is_refused_everywhere_else(client, setup) -> None:
    for path in ("/api/tags", "/api/generic", "/api/tag-connector/v1", "/api/tag-connector/v2/whoami"):
        r = client.get(path, headers=_auth(setup["c1"]))
        assert r.status_code == 401, path
        assert r.json()["error"] == "tag_credential_not_accepted", path
    assert client.post("/", headers=_auth(setup["auth"])).status_code == 401


def test_revoked_and_rotated_credentials(client, setup, tagenv) -> None:
    from app.tags import service

    cred, secret, revoked = run(service.rotate_companion(setup["companion_id"], created_by="admin"))
    assert revoked == [setup["credential_id"]]
    r = client.get(f"{V1}/whoami", headers=_auth(setup["auth"]))
    assert r.status_code == 401 and r.json()["error"] == "credential_revoked"
    assert client.get(f"{V1}/whoami", headers=_auth(f"CremindTag {cred['id']}.{secret}")).status_code == 200
    content_id = setup["c1"].split(" ", 1)[1].split(".", 1)[0]
    run(tagenv.store.revoke_credential(content_id, profile="p1", kind="content"))
    r = client.post(f"{V1}/sync", headers=_auth(setup["c1"]), json={})
    assert r.status_code == 401 and r.json()["error"] == "credential_revoked"


def test_kinds_cannot_cross(client, setup) -> None:
    hardware_calls = [("post", "inventory", {}), ("post", "heartbeat", {}), ("get", "commands", None),
                      ("post", "commands/x/claim", None), ("post", "commands/x/result", {"status": "failed"})]
    for method, path, body in hardware_calls:
        r = getattr(client, method)(f"{V1}/{path}", headers=_auth(setup["c1"]),
                                    **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["error"] == "wrong_credential_kind", path
    content_calls = [("post", "sync", {}), ("get", "events", None), ("post", "accepted", {"delivery_ids": []}),
                     ("post", "receipts", {"receipts": []}), ("post", "previews", {})]
    for method, path, body in content_calls:
        r = getattr(client, method)(f"{V1}/{path}", headers=_auth(setup["auth"]),
                                    **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["error"] == "wrong_credential_kind", path


def test_content_credentials_die_with_their_profile(setup, tagenv) -> None:
    content_id = setup["c2"].split(" ", 1)[1].split(".", 1)[0]
    with tagenv.engine.begin() as c:
        c.execute(text("DELETE FROM profiles WHERE name='p2'"))
    assert run(tagenv.store.get_credential(content_id)) is None


# ── content ─────────────────────────────────────────────────────────────────


def test_the_profile_comes_from_the_credential(client, setup, tagenv) -> None:
    _append(tagenv, "p1", 2)
    _append(tagenv, "p2", 1)
    r = client.post(f"{V1}/sync", headers=_auth(setup["c2"]), json={"cursor": 0, "profile": "p1"})
    body = r.json()
    assert body["profile"] == "p2"
    assert [t["tag_id"] for t in body["tags"]] == ["T2"]
    assert [j["tag_id"] for j in body["outstanding"]] == ["T2"]
    assert body["settings"]["language"] == "en" and body["cursor_valid"] is True
    ev = client.get(f"{V1}/events", headers=_auth(setup["c1"]), params={"after": 0}).json()
    assert [j["tag_id"] for j in ev["jobs"]] == ["T1", "T1"]
    assert ev["next_after"] == ev["head_seq"] == 2
    job = ev["jobs"][0]
    assert set(job) == {"delivery_id", "seq", "tag_id", "epoch", "kind", "priority", "replace_key",
                        "resolves", "created_at", "expires_at", "stage", "card"}
    assert job["card"]["v"] == 1 and job["created_at"].endswith("Z")


def test_a_content_credential_is_bound_to_its_companion(client, setup, tagenv) -> None:
    other = hardware(tagenv, tags=("T9",), bridges=("B9",))
    claim(tagenv, other["tags"]["T9"], "p1")
    _append(tagenv, "p1", 1)
    ev = client.get(f"{V1}/events", headers=_auth(setup["c1"]), params={"after": 0}).json()
    assert [j["tag_id"] for j in ev["jobs"]] == ["T1"]
    ev9 = client.get(f"{V1}/events", headers=_auth(content_credential(tagenv, "p1", other["companion_id"])),
                     params={"after": 0}).json()
    assert [j["tag_id"] for j in ev9["jobs"]] == ["T9"]


def test_events_paging_and_cursor_expiry(client, setup, tagenv) -> None:
    _append(tagenv, "p1", 3)
    h = _auth(setup["c1"])
    page = client.get(f"{V1}/events", headers=h, params={"after": 0, "limit": 2}).json()
    assert [j["seq"] for j in page["jobs"]] == [1, 2] and page["next_after"] == 2
    page = client.get(f"{V1}/events", headers=h, params={"after": 2, "limit": 2}).json()
    assert [j["seq"] for j in page["jobs"]] == [3] and page["next_after"] == 3
    r = client.get(f"{V1}/events", headers=h, params={"after": 99})
    assert r.status_code == 410 and r.json()["error"] == "cursor_expired" and r.json()["head_seq"] == 3
    run(tagenv.store.save_settings("p1"))
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_streams SET state='{\"pruned_through_seq\": 2}' WHERE profile='p1'"))
    r = client.get(f"{V1}/events", headers=h, params={"after": 1})
    assert r.status_code == 410 and r.json()["oldest_seq"] == 3
    assert client.get(f"{V1}/events", headers=h, params={"after": 2}).status_code == 200
    sync = client.post(f"{V1}/sync", headers=h, json={"cursor": 1}).json()
    assert sync["cursor_valid"] is False and sync["oldest_seq"] == 3
    assert client.get(f"{V1}/events", headers=h, params={"after": "x"}).status_code == 400


def test_accepted_and_monotonic_receipts(client, setup, tagenv) -> None:
    _append(tagenv, "p1", 1)
    _append(tagenv, "p2", 1)
    h = _auth(setup["c1"])
    job = client.get(f"{V1}/events", headers=h, params={"after": 0}).json()["jobs"][0]
    other = client.get(f"{V1}/events", headers=_auth(setup["c2"]), params={"after": 0}).json()["jobs"][0]
    did = job["delivery_id"]
    r = client.post(f"{V1}/accepted", headers=h, json={"through_seq": 1, "delivery_ids": [did, other["delivery_id"]]})
    assert r.json() == {"accepted": 1}
    stage = lambda i: scalar(tagenv, "SELECT stage FROM tag_deliveries WHERE id=:i", i=i)  # noqa: E731
    assert stage(did) == "companion_accepted" and stage(other["delivery_id"]) == "queued"

    def receipt(**kw):
        return client.post(f"{V1}/receipts", headers=h, json={"receipts": [{"delivery_id": did, **kw}]}).json()

    def rejected(reason):
        return {"applied": 0, "rejected": [{"delivery_id": did, "reason": reason}]}

    assert receipt(stage="bridge_received", epoch=job["epoch"]) == {"applied": 1, "rejected": []}
    assert receipt(stage="companion_accepted") == {"applied": 0, "rejected": []}  # never backwards
    assert receipt(stage="refreshing", epoch=job["epoch"] + 5) == rejected("epoch_mismatch")
    assert receipt(stage="displayed", outcome="displayed", revision=7, digest="abcd1234",
                   at="2026-09-27T10:00:00Z", timing={"wake_ms": 1}) == {"applied": 1, "rejected": []}
    # A repeat of the final receipt is a quiet no-op; another outcome is refused.
    assert receipt(stage="displayed", outcome="displayed", revision=7) == {"applied": 0, "rejected": []}
    assert receipt(outcome="failed") == rejected("terminal")
    assert stage(did) == "displayed"
    dev = run(tagenv.store.get_device(setup["tags"]["T1"]))
    assert dev["displayed_revision"] == 7 and dev["displayed_digest"] == "abcd1234"
    # Another profile's delivery is untouched by this credential.
    r = client.post(f"{V1}/receipts", headers=h,
                    json={"receipts": [{"delivery_id": other["delivery_id"], "outcome": "failed"},
                                       {"stage": "displayed"}]})
    assert r.json() == {"applied": 0, "rejected": [
        {"delivery_id": other["delivery_id"], "reason": "unknown"},
        {"delivery_id": None, "reason": "invalid"},
    ]}
    assert stage(other["delivery_id"]) == "queued"


def test_previews(client, setup, tagenv) -> None:
    h = _auth(setup["c1"])
    good = {"tag_id": "T1", "revision": 3, "kind": "displayed",
            "png_base64": base64.b64encode(PNG).decode(), "delivery_ids": [1]}
    assert client.post(f"{V1}/previews", headers=h, json=good).json() == {"stored": True}
    assert client.post(f"{V1}/previews", headers=h, json={**good, "revision": 2}).json() == {"stored": False}
    big = base64.b64encode(PNG + b"\x00" * (64 * 1024)).decode()
    r = client.post(f"{V1}/previews", headers=h, json={**good, "png_base64": big})
    assert r.status_code == 422 and r.json()["error"] == "preview_too_large"
    r = client.post(f"{V1}/previews", headers=h, json={**good, "png_base64": base64.b64encode(b"GIF89a").decode()})
    assert r.status_code == 422 and r.json()["error"] == "invalid_preview"
    r = client.post(f"{V1}/previews", headers=h, json={**good, "tag_id": "T2"})  # p2's tag
    assert r.status_code == 404
    from app.config.settings import BaseConfig

    token, _ = BaseConfig.mint_token("p1")
    img = client.get(f"/api/tags/devices/{setup['tags']['T1']}/preview",
                     headers=_auth(f"Bearer {token}"), params={"kind": "displayed"})
    assert img.status_code == 200 and img.content == PNG and img.headers["x-tag-revision"] == "3"


# ── hardware ────────────────────────────────────────────────────────────────


def test_inventory_and_heartbeat(client, setup, tagenv) -> None:
    h = _auth(setup["auth"])
    inv = client.post(f"{V1}/inventory", headers=h, json={
        "gateways": [], "bridges": [{"hw_id": "B1", "fontpack_id": "a1b2"}],
        "tags": [{"tag_id": "T1", "width": 296, "height": 128, "planes": 2}, {"tag_id": "T7"}],
    }).json()
    kinds = {(d["kind"], d["hw_id"]) for d in inv["devices"]}
    assert ("tag", "T7") in kinds and ("gateway", "gw-1") in kinds
    t1 = next(a for a in inv["assignments"] if a["tag_id"] == "T1")
    assert t1 == {"tag_id": "T1", "owner_profile": "p1", "bridge_hw_id": "B1", "epoch": 1, "rotation": 0}
    assert run(tagenv.store.get_device(setup["tags"]["T1"]))["width"] == 296
    hb = client.post(f"{V1}/heartbeat", headers=h, json={
        "companion": {"version": "0.1.0", "host": "desk-pc"}, "queue": {"depth": 0},
        "devices": [{"hw_id": "T1", "kind": "tag", "battery_mv": 2900, "rssi": -61,
                     "last_contact_at": "2026-09-27T10:00:00Z", "status": "ok"}],
    }).json()
    assert set(hb) == {"server_time", "commands_pending"}
    dev = run(tagenv.store.get_device(setup["tags"]["T1"]))
    assert dev["battery_mv"] == 2900 and dev["status"] == "ok"
    assert run(tagenv.store.get_companion(setup["companion_id"]))["host"] == "desk-pc"


def test_commands_claim_and_result(client, setup, tagenv) -> None:
    h = _auth(setup["auth"])
    listed = client.get(f"{V1}/commands", headers=h).json()["commands"]
    assert {c["kind"] for c in listed} == {"assign_tag"}  # the clears were completed by the fixture
    cid = listed[0]["id"]
    claimed = client.post(f"{V1}/commands/{cid}/claim", headers=h)
    assert claimed.status_code == 200 and claimed.json()["id"] == cid and claimed.json()["status"] == "claimed"
    again = client.post(f"{V1}/commands/{cid}/claim", headers=h)
    assert again.status_code == 409 and again.json()["error"] == "already_claimed"
    ok = client.post(f"{V1}/commands/{cid}/result", headers=h, json={"status": "succeeded", "result": {"x": 1}})
    assert ok.status_code == 200 and ok.json()["command"]["status"] == "succeeded"
    assert client.post(f"{V1}/commands/{cid}/result", headers=h, json={"status": "succeeded"}).status_code == 200
    clash = client.post(f"{V1}/commands/{cid}/result", headers=h, json={"status": "failed"})
    assert clash.status_code == 409 and clash.json()["error"] == "already_completed"
    other = hardware(tagenv, tags=(), bridges=())
    r = client.post(f"{V1}/commands/{cid}/claim", headers=_auth(other["auth"]))
    assert r.status_code == 404  # another companion's command does not exist for it


def test_long_poll_returns_when_a_command_is_queued(tagenv, setup) -> None:
    from app.api.tag_connector import get_tag_connector_routes

    handler = find_handler(get_tag_connector_routes(), f"{V1}/commands", "GET")
    for c in run(tagenv.store.list_commands(companion_id=setup["companion_id"], statuses=("queued",))):
        run(tagenv.store.complete_command(setup["companion_id"], c["id"], status="failed",
                                          result=None, error="x"))

    async def go():
        req = make_request(None, query={"wait": "10"}, headers={"Authorization": setup["auth"]})
        started = time.monotonic()
        poll = asyncio.create_task(handler(req))
        await asyncio.sleep(0.3)
        assert not poll.done()
        await tagenv.store.create_command(companion_id=setup["companion_id"], kind="identify",
                                          args={"hw_id": "T1"}, requested_by="t", ttl_s=60)
        resp = await asyncio.wait_for(poll, timeout=5)
        return resp, time.monotonic() - started

    resp, elapsed = run(go())
    import json as _json

    assert [c["kind"] for c in _json.loads(resp.body)["commands"]] == ["identify"]
    assert elapsed < 5
