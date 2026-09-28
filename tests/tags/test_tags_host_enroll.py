"""Enrolling a desktop gateway computer (``enroll_host`` setup sessions).

The Cremind page starts a session whose link is ``cremind://tags/setup?…``;
the computer binds with its installation key, the person approves there and
confirms the same four words on the page (the same sign-in), and the computer
redeems with the SHA-256 of a credential secret it keeps. What is pinned: the
secret never reaches the server, the credential is scoped to that computer
and that profile, enrolling again replaces it, another profile never sees the
computer, and its owner (or the computer itself) removes it.
"""

from __future__ import annotations

import hashlib

from tests.tags._helpers import find_handler, run, scalar, tagenv  # noqa: F401
from tests.tags.test_tags_setup import ORIGIN, Connect, _authority_dir, call, req  # noqa: F401

SECRET = "k" * 43


def _profile_routes():
    from app.api.tags_setup import get_tags_setup_routes

    return get_tags_setup_routes()


def start(profile: str = "p1") -> tuple[str, str]:
    status, out = call(_profile_routes(), "POST", "/api/tags/setup-sessions",
                       req(profile, body={"operation": "enroll_host", "server_url": ORIGIN}))
    assert status == 201, out
    return out["session"]["id"], out["launch_url"]


def session_of(profile: str, session_id: str) -> dict:
    status, out = call(_profile_routes(), "GET", "/api/tags/setup-sessions/{session_id}",
                       req(profile, path={"session_id": session_id}))
    assert status == 200, out
    return out["session"]


def confirm(profile: str, session_id: str) -> tuple[int, dict]:
    return call(_profile_routes(), "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                req(profile, path={"session_id": session_id}, body={}))


def enroll(profile: str = "p1", computer: Connect | None = None, secret: str = SECRET,
           key: str = "redeem-key-0001") -> tuple[Connect, str, dict]:
    computer = computer or Connect("LAPTOP-9")
    session_id, link = start(profile)
    assert link.startswith("cremind://tags/setup?v=1&server=https%3A%2F%2Fcremind.test%3A1180&session=")
    computer.adopt(link)
    computer.nonce = ""  # a new session: bind is signed before the server's nonce exists
    status, bound = computer.post("bind", {"installation": computer.installation()})
    assert status == 200, bound
    computer.nonce = bound["server_nonce"]
    assert bound["session"]["operation"] == "enroll_host" and bound["profile"]["name"] == profile
    assert len(bound["verification_phrase"].split()) == 4
    status, out = computer.post("approve", {})
    assert status == 200 and out["state"] == "waiting_for_confirmation", out
    assert session_of(profile, session_id)["verification_phrase"] == bound["verification_phrase"]
    status, out = confirm(profile, session_id)
    assert status == 200 and out["session"]["state"] == "redeeming", out
    status, result = computer.post("redeem", {"idempotency_key": key,
                                              "credential_sha256": hashlib.sha256(secret.encode()).hexdigest()})
    assert status == 200, result
    return computer, session_id, result


def test_a_desktop_computer_enrolls_and_gets_a_credential_only_it_knows(tagenv) -> None:
    from app.tags import hosts

    computer, session_id, result = enroll()
    assert result["credential_id"].startswith("tagh_") and result["host"]["name"] == "LAPTOP-9"
    assert result["profile"] == {"name": "p1", "id": "pid1"} and result["server"]["origin"] == ORIGIN
    assert SECRET not in str(result), "the secret never reaches Cremind"
    stored = scalar(tagenv, "SELECT secret_sha256 FROM tag_host_credentials WHERE id = :i", i=result["credential_id"])
    assert stored == hashlib.sha256(SECRET.encode()).hexdigest()
    session = session_of("p1", session_id)
    assert session["state"] == "completed" and session["host_id"] == result["host_id"]
    principal = run(hosts.authenticate_host(result["credential_id"], SECRET))
    assert (principal.host_id, principal.kind, principal.profile) == (result["host_id"], "desktop", "p1")

    # A repeat of the same redeem answers the same; another key is refused.
    status, again = computer.post("redeem", {"idempotency_key": "redeem-key-0001",
                                             "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest()})
    assert status == 200 and again["credential_id"] == result["credential_id"]
    status, out = computer.post("redeem", {"idempotency_key": "another-key-0002",
                                           "credential_sha256": hashlib.sha256(b"x").hexdigest()})
    assert status == 409 and out["error"] == "already_redeemed"

    # Only its profile sees it; the other profile cannot even search it.
    mine = run(hosts.list_hosts("p1"))["hosts"]
    assert [h["id"] for h in mine if h["kind"] == "desktop"] == [result["host_id"]]
    assert not [h for h in run(hosts.list_hosts("p2"))["hosts"] if h["kind"] == "desktop"]
    assert not [h for h in run(hosts.list_hosts("admin"))["hosts"] if h["kind"] == "desktop"]


def test_the_computer_must_wait_for_the_pages_confirmation(tagenv) -> None:
    computer = Connect("LAPTOP-9")
    session_id, link = start()
    computer.adopt(link)
    status, bound = computer.post("bind", {"installation": computer.installation()})
    computer.nonce = bound["server_nonce"]
    status, out = computer.post("redeem", {"idempotency_key": "redeem-key-0001",
                                           "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest()})
    assert status == 409 and out["error"] == "not_confirmed"
    status, out = confirm("p1", session_id)
    assert status == 409 and out["error"] == "not_approved", "approve on the computer first"
    # Another sign-in of the same profile cannot confirm it.
    status, out = call(_profile_routes(), "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                       req("p1", path={"session_id": session_id}, body={},
                           headers={"authorization": "Bearer another-sign-in"}))
    assert status == 403 and out["error"] == "session_mismatch"
    # A redeem without a credential hash is refused.
    computer.post("approve", {})
    confirm("p1", session_id)
    status, out = computer.post("redeem", {"idempotency_key": "redeem-key-0001"})
    assert status == 422 and out["error"] == "invalid_redeem"


def test_enrolling_again_keeps_the_computer_and_replaces_its_credential(tagenv) -> None:
    from app.tags import hosts
    from app.tags.service import TagError

    computer, _, first = enroll()
    _, _, second = enroll(computer=computer, secret="n" * 43, key="redeem-key-0002")
    assert second["host_id"] == first["host_id"] and second["credential_id"] != first["credential_id"]
    try:
        run(hosts.authenticate_host(first["credential_id"], SECRET))
        raise AssertionError("the old credential still works")
    except TagError as exc:
        assert exc.code == "credential_revoked"
    assert run(hosts.authenticate_host(second["credential_id"], "n" * 43)).host_id == first["host_id"]
    # The same computer set up by another profile is another gateway computer of that profile.
    _, _, other = enroll("p2", computer=computer, secret="m" * 43, key="redeem-key-0003")
    assert other["host_id"] != first["host_id"]


def test_the_owner_or_the_computer_removes_it(tagenv) -> None:
    from app.api.tag_host import get_tag_host_routes
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.tags import hosts
    from app.tags.service import TagError

    _, _, result = enroll()
    routes = get_tags_hosts_routes()
    status, out = call(routes, "DELETE", "/api/tags/hosts/{host_id}", req("p2", path={"host_id": result["host_id"]}))
    assert status == 404
    status, out = call(routes, "DELETE", "/api/tags/hosts/{host_id}", req("p1", path={"host_id": result["host_id"]}))
    assert status == 200 and out["host"]["state"] == "revoked", out
    assert not [h for h in run(hosts.list_hosts("p1"))["hosts"] if h["kind"] == "desktop"]
    try:
        run(hosts.authenticate_host(result["credential_id"], SECRET))
        raise AssertionError("a removed computer's credential still works")
    except TagError as exc:
        assert exc.code == "credential_revoked"

    # The computer can leave by itself (cremind tags host forget).
    computer, _, again = enroll(computer=Connect("DESK-2"), secret="z" * 43, key="redeem-key-0004")
    status, out = call(get_tag_host_routes(), "POST", "/api/tag-host/v1/leave",
                       req(None, body={}, headers={"authorization": f"CremindHost {again['credential_id']}.{'z' * 43}"}))
    assert status == 200 and out["host"]["state"] == "revoked", out
    status, out = call(get_tag_host_routes(), "POST", "/api/tag-host/v1/hello",
                       req(None, body={}, headers={"authorization": f"CremindHost {again['credential_id']}.{'z' * 43}"}))
    assert status == 401


def test_the_servers_own_computer_cannot_be_removed(tagenv) -> None:
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.tags import hosts

    run(hosts.host_hello(hosts.HostPrincipal("host-srv", hosts.SERVER), {"name": "Office PC", "status": {}}))
    status, out = call(get_tags_hosts_routes(), "DELETE", "/api/tags/hosts/{host_id}",
                       req("admin", path={"host_id": "host-srv"}))
    assert status == 409 and out["error"] == "host_not_removable"
