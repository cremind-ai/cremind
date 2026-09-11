"""The OAuth return store and its two routes (app/api/oauth_return.py).

A consent tab has to find its way back to the page that opened it without the
return URL ever carrying the authorization code or the OAuth state. These pin
the three steps (record -> complete -> consume), their one-time and expiry
semantics, the profile isolation (first writer wins, routes coerced under the
recording profile), and that nothing replayable is written to disk.
"""
import asyncio
import os
import re
import time
from types import SimpleNamespace

import jwt
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

import app.api.oauth_callback as oc
import app.api.oauth_return as oret
from app.config.settings import BaseConfig
from app.server import JWTAuthBackend

SECRET = "oauth-return-test-secret-only-32plus"
STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"  # 30 chars — matches _STATE_RE
OTHER_STATE = "Q2hhbmdlZC1zdGF0ZS1mb3ItdGVzdHM"
CODE = "4/0AVGzR1CsecretAuthorizationCode"


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path / "oauth_returns"


def _advance(monkeypatch, seconds: float) -> None:
    """Move the store's clock (records are judged by it, not the wall clock)."""
    later = time.time() + seconds
    monkeypatch.setattr(oret, "_now", lambda: later)


def _files(folder):
    return [os.path.join(root, name) for root, _dirs, names in os.walk(folder) for name in names]


# ── record -> complete -> consume ───────────────────────────────────────────

def test_a_recorded_page_comes_back_through_a_one_time_ref(store):
    route = "/alice/settings/gsuite?tab=drive"
    recorded = oret.record_context(STATE, profile="alice", route=route, flow="drive")
    assert recorded["recorded"] is True
    assert recorded["expires_at"] > time.time()

    ref = oret.complete(STATE, outcome="received", flow="drive")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", ref)
    assert oret.consume(ref) == {
        "outcome": "received", "flow": "drive", "profile": "alice",
        "route": route, "message": None,
    }


def test_a_ref_can_be_consumed_only_once(store):
    ref = oret.complete(STATE, outcome="received", flow="skill")
    oret.consume(ref)
    with pytest.raises(LookupError):
        oret.consume(ref)


def test_a_replayed_callback_gets_a_context_less_ref(store):
    """complete() claims the context, so the same state again steers nobody."""
    oret.record_context(STATE, profile="alice", route="/alice/calendar", flow="calendar")
    first = oret.consume(oret.complete(STATE, outcome="received", flow="calendar"))
    replay = oret.consume(oret.complete(STATE, outcome="received", flow="calendar"))
    assert (first["profile"], first["route"]) == ("alice", "/alice/calendar")
    assert (replay["profile"], replay["route"]) == (None, None)
    assert replay["outcome"] == "received"


def test_an_unrecorded_flow_still_reports_its_outcome(store):
    ref = oret.complete(STATE, outcome="denied", flow="skill",
                        message="Authorization was not granted (access_denied).")
    assert oret.consume(ref) == {
        "outcome": "denied", "flow": "skill", "profile": None, "route": None,
        "message": "Authorization was not granted (access_denied).",
    }


def test_the_callback_flow_wins_over_the_recorded_one(store):
    """Which callback Google actually hit is the truth, not the recorder's guess."""
    oret.record_context(STATE, profile="alice", route="/alice", flow="skill")
    assert oret.consume(oret.complete(STATE, outcome="received", flow="calendar"))["flow"] == "calendar"


def test_unknown_and_malformed_refs_are_lookup_errors(store):
    for ref in (None, "", "short", "x" * 43 + "!", 42, "A" * 43, "A" * 43 + "\n"):
        with pytest.raises(LookupError):
            oret.consume(ref)


def test_complete_refuses_an_unknown_outcome_or_flow(store):
    with pytest.raises(ValueError):
        oret.complete(STATE, outcome="linked", flow="skill")
    with pytest.raises(ValueError):
        oret.complete(STATE, outcome="received", flow="gmail")


# ── expiry ──────────────────────────────────────────────────────────────────

def test_a_context_expires_after_ten_minutes(store, monkeypatch):
    oret.record_context(STATE, profile="alice", route="/alice/calendar", flow="calendar")
    _advance(monkeypatch, oret.TTL_S + 5)
    result = oret.consume(oret.complete(STATE, outcome="received", flow="calendar"))
    assert (result["profile"], result["route"]) == (None, None)


def test_a_ref_expires_after_ten_minutes(store, monkeypatch):
    ref = oret.complete(STATE, outcome="received", flow="skill")
    _advance(monkeypatch, oret.TTL_S + 5)
    with pytest.raises(LookupError):
        oret.consume(ref)


def test_expired_records_are_pruned_on_the_next_write(store, monkeypatch):
    oret.record_context(STATE, profile="alice", route="/alice", flow="skill")
    stale = oret._context_path(STATE)
    assert stale.exists()
    _advance(monkeypatch, oret.TTL_S + 5)
    oret.record_context(OTHER_STATE, profile="alice", route="/alice", flow="skill")
    assert not stale.exists()


# ── profile isolation (two profiles, not just admin) ────────────────────────

def test_the_first_profile_to_record_a_state_keeps_it(store):
    oret.record_context(STATE, profile="alice", route="/alice/calendar", flow="calendar")
    with pytest.raises(PermissionError):
        oret.record_context(STATE, profile="bob", route="/bob/settings", flow="calendar")
    # The same profile re-recording simply overwrites.
    oret.record_context(STATE, profile="alice", route="/alice/settings", flow="calendar")
    result = oret.consume(oret.complete(STATE, outcome="received", flow="calendar"))
    assert (result["profile"], result["route"]) == ("alice", "/alice/settings")


def test_an_expired_claim_does_not_block_another_profile(store, monkeypatch):
    oret.record_context(STATE, profile="alice", route="/alice", flow="skill")
    _advance(monkeypatch, oret.TTL_S + 5)
    oret.record_context(STATE, profile="bob", route="/bob", flow="skill")
    assert oret.consume(oret.complete(STATE, outcome="received", flow="skill"))["profile"] == "bob"


@pytest.mark.parametrize("route", [
    "/bob/settings",        # another profile's prefix
    "/",                    # no profile at all
    "/alicex/settings",     # a prefix of the name is not the name
    "/alice/../bob/settings",
    "/alice/%2e%2e/bob",    # an encoded dot segment a router could resolve
    "/alice/./settings",
])
def test_a_route_outside_the_recording_profile_is_coerced_to_its_home(store, route):
    oret.record_context(STATE, profile="alice", route=route, flow="skill")
    result = oret.consume(oret.complete(STATE, outcome="received", flow="skill"))
    assert (result["profile"], result["route"]) == ("alice", "/alice")


@pytest.mark.parametrize("route", [
    "//evil.example/alice",
    "/alice\\..\\bob",
    "/alice/" + "a" * 2100,
    "/alice/\nsettings",
    "alice/settings",
    "https://evil.example/alice",
    None,
    42,
    ["/alice"],
])
def test_a_structurally_unusable_route_is_refused(store, route):
    with pytest.raises(ValueError):
        oret.record_context(STATE, profile="alice", route=route, flow="skill")
    assert not store.exists() or not any(store.iterdir())


@pytest.mark.parametrize("state", ["short", "has a space in it", "../../../etc/passwd", "", None, 7,
                                   STATE + "\n"])
def test_an_invalid_state_is_refused(store, state):
    with pytest.raises(ValueError):
        oret.record_context(state, profile="alice", route="/alice", flow="skill")


def test_an_invalid_profile_or_flow_is_refused(store):
    for profile in ("", "Alice", "bob/../admin", "alice\n", None):
        with pytest.raises(ValueError):
            oret.record_context(STATE, profile=profile, route="/alice", flow="skill")
    for flow in ("gmail", "", None, ["skill"]):
        with pytest.raises(ValueError):
            oret.record_context(STATE, profile="alice", route="/alice", flow=flow)


def test_the_state_rule_is_the_callbacks_own():
    """Duplicated to avoid a circular import; must never drift."""
    assert oret._STATE_RE.pattern == oc._STATE_RE.pattern


# ── nothing replayable at rest ──────────────────────────────────────────────

def test_neither_the_state_nor_the_code_nor_the_ref_is_written_to_disk(store):
    """End to end through the real callback: the inbox holds the query by design
    (the skill replays it), the return store must hold none of it."""
    oret.record_context(STATE, profile="alice", route="/alice/c/abc", flow="skill")
    query = f"state={STATE}&code={CODE.replace('/', '%2F')}&scope=email+openid"
    request = SimpleNamespace(query_params={"state": STATE, "code": CODE, "scope": "email openid"},
                              url=SimpleNamespace(query=query))
    response = asyncio.run(oc._handle_inbox_callback(request))
    ref = response.headers["location"].split("ref=", 1)[1]

    files = _files(store)
    assert files, "the ref record should be on disk until consumed"
    for path in files:
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        for secret in (STATE, CODE, CODE.replace("/", "%2F"), ref):
            assert secret not in name
            assert secret not in content
        if os.name != "nt":
            assert os.stat(path).st_mode & 0o077 == 0
    assert oret.consume(ref)["route"] == "/alice/c/abc"


def test_the_store_is_bounded(store, monkeypatch):
    monkeypatch.setattr(oret, "MAX_RECORDS", 5)
    base = time.time() - 60
    states = [f"bounded-state-{i:04d}" for i in range(12)]
    for i, state in enumerate(states):
        oret.record_context(state, profile="alice", route="/alice", flow="skill")
        # Distinct, ordered mtimes: eviction is oldest-first.
        os.utime(oret._context_path(state), (base + i, base + i))
    assert len([p for p in store.iterdir() if p.suffix == ".json"]) <= 5
    assert oret.consume(oret.complete(states[-1], outcome="received", flow="skill"))["profile"] == "alice"
    assert oret.consume(oret.complete(states[0], outcome="received", flow="skill"))["profile"] is None


# ── redirects ───────────────────────────────────────────────────────────────

def test_the_return_redirect_carries_only_the_ref():
    ref = "A" * 43
    response = oret.return_redirect(ref)
    assert response.status_code == 303
    assert response.headers["location"] == f"/#/oauth-return?ref={ref}"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_invalid_state_redirect():
    response = oret.invalid_state_redirect()
    assert response.status_code == 303
    assert response.headers["location"] == "/#/oauth-return?error=invalid_state"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


# ── HTTP routes ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr("app.auth.tokens.serial_matches", lambda claims: claims.get("tsr") == 3)
    app = Starlette(routes=oret.get_oauth_return_routes(), middleware=[
        Middleware(AuthenticationMiddleware, backend=JWTAuthBackend(lambda: SECRET)),
    ])
    return TestClient(app)


def _auth(profile: str) -> dict:
    from app.auth.tokens import current_transport_epoch
    token = jwt.encode({"sub": profile, "profile": profile, "tsr": 3, "tep": current_transport_epoch(),
                        "iat": int(time.time()) - 1, "exp": int(time.time()) + 3600}, SECRET)
    return {"Authorization": f"Bearer {token}"}


def test_recording_a_context_needs_a_session(client, store):
    response = client.post("/api/oauth/return/context",
                           json={"state": STATE, "route": "/alice", "flow": "skill"})
    assert response.status_code == 401
    assert not store.exists()


def test_recording_is_keyed_to_the_calling_profile(client):
    ok = client.post("/api/oauth/return/context", headers=_auth("alice"),
                     json={"state": STATE, "route": "/alice/settings/gsuite", "flow": "drive"})
    assert ok.status_code == 200
    assert ok.json()["recorded"] is True
    assert isinstance(ok.json()["expires_at"], int)
    assert ok.headers["cache-control"] == "no-store"

    clash = client.post("/api/oauth/return/context", headers=_auth("bob"),
                        json={"state": STATE, "route": "/bob/settings/gsuite", "flow": "drive"})
    assert clash.status_code == 409
    assert clash.json() == {"error": "conflict"}

    result = oret.consume(oret.complete(STATE, outcome="received", flow="drive"))
    assert (result["profile"], result["route"]) == ("alice", "/alice/settings/gsuite")


def test_a_route_under_another_profile_is_recorded_under_the_caller(client):
    assert client.post("/api/oauth/return/context", headers=_auth("alice"),
                       json={"state": STATE, "route": "/bob/settings"}).status_code == 200
    result = oret.consume(oret.complete(STATE, outcome="received", flow="skill"))
    assert (result["profile"], result["route"]) == ("alice", "/alice")


def test_flow_defaults_to_skill(client):
    assert client.post("/api/oauth/return/context", headers=_auth("alice"),
                       json={"state": STATE, "route": "/alice"}).status_code == 200
    assert oret._read(oret._context_path(STATE))["flow"] == "skill"


@pytest.mark.parametrize("body", [
    b"not json",
    b"",
    b"[1, 2]",
])
def test_recording_rejects_a_body_that_is_not_an_object(client, body):
    response = client.post("/api/oauth/return/context", headers=_auth("alice"), content=body)
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [
    {"route": "/alice"},                                   # no state
    {"state": "bad state", "route": "/alice"},
    {"state": STATE},                                      # no route
    {"state": STATE, "route": "//evil.example"},
    {"state": STATE, "route": "/alice", "flow": "gmail"},
    {"state": STATE, "route": "/alice", "flow": ["skill"]},
])
def test_recording_rejects_unusable_fields(client, payload):
    response = client.post("/api/oauth/return/context", headers=_auth("alice"), json=payload)
    assert response.status_code == 400
    assert "error" in response.json()


def test_consuming_needs_no_session_and_works_once(client):
    oret.record_context(STATE, profile="alice", route="/alice/calendar", flow="calendar")
    ref = oret.complete(STATE, outcome="received", flow="calendar")

    first = client.post("/api/oauth/return/consume", json={"ref": ref})
    assert first.status_code == 200
    assert first.json() == {"outcome": "received", "flow": "calendar", "profile": "alice",
                            "route": "/alice/calendar", "message": None}
    assert first.headers["cache-control"] == "no-store"

    again = client.post("/api/oauth/return/consume", json={"ref": ref})
    assert again.status_code == 404
    assert again.json() == {"error": "unknown"}


def test_consuming_an_unknown_ref_is_a_404(client):
    assert client.post("/api/oauth/return/consume", json={"ref": "A" * 43}).status_code == 404
    assert client.post("/api/oauth/return/consume", json={"ref": ["x"]}).status_code == 404
    assert client.post("/api/oauth/return/consume", json={}).status_code == 404
    assert client.post("/api/oauth/return/consume", content=b"nope").status_code == 400
