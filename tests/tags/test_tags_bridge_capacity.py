"""Bridge capacity: a bridge's assignment table holds a fixed number of tags
(nRF52832: 10, nRF52840: 20).

The companion reports ``max_tags`` / ``assigned`` per bridge in its inventory;
claim and assign refuse a full bridge (409 ``bridge_full``); a bridge that
refuses a tag anyway fails the ``assign_tag`` command and the tag reads
``assign_failed``. Runs on SQLite and, through ``tagenv``, on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.api.tag_connector import get_tag_connector_routes  # noqa: E402
from app.api.tags_hardware import get_tags_hardware_routes  # noqa: E402
from app.tags import service  # noqa: E402
from app.tags.service import TagError  # noqa: E402
from tests.tags._helpers import body_of, claim, find_handler, hardware, make_request, rows, run, scalar  # noqa: E402

V1 = "/api/tag-connector/v1"
CLAIM = "/api/tags/hardware/tags/{device_id}/claim"
ASSIGN = "/api/tags/hardware/tags/{device_id}/assign"
RESULT = "commands/{command_id}/result"


def admin(route_path, method, **kw):
    handler = find_handler(get_tags_hardware_routes(), route_path, method)
    return run(handler(make_request("admin", **kw)))


def connector(route_path, method, auth, **kw):
    handler = find_handler(get_tag_connector_routes(), f"{V1}/{route_path}", method)
    return run(handler(make_request(None, headers={"Authorization": auth}, **kw)))


def report(tagenv, hw, bridges, tags=()):
    """A connector inventory report of these bridges (hw_id -> extra fields)."""
    body = {"bridges": [{"hw_id": b, **extra} for b, extra in bridges.items()],
            "tags": [{"tag_id": t} for t in tags]}
    resp = connector("inventory", "POST", hw["auth"], body=body)
    assert resp.status_code == 200, resp.body
    return body_of(resp)


def info(tagenv, device_id):
    raw = scalar(tagenv, "SELECT info FROM tag_devices WHERE id=:i", i=device_id)
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def device(tagenv, device_id):
    return run(tagenv.store.get_device(device_id))


def assign_command(tagenv, tag_hw):
    found = [r for r in rows(tagenv, "SELECT id, status, args FROM tag_commands WHERE kind='assign_tag'")
             if json.loads(r["args"])["tag_id"] == tag_hw]
    return found[-1] if found else None


# ── (1) inventory ───────────────────────────────────────────────────────────


def test_inventory_keeps_a_bridges_capacity_and_drops_bad_values(tagenv) -> None:
    hw = hardware(tagenv, tags=(), bridges=("B1", "B2"))
    b1, b2 = hw["bridges"]["B1"], hw["bridges"]["B2"]
    out = report(tagenv, hw, {"B1": {"max_tags": 10, "assigned": 3}, "B2": {"max_tags": 20, "assigned": 0}})
    by_id = {d["id"]: d for d in out["devices"]}
    assert by_id[b1]["info"]["max_tags"] == 10 and by_id[b1]["info"]["assigned"] == 3
    assert info(tagenv, b2)["max_tags"] == 20 and info(tagenv, b2)["assigned"] == 0
    assert info(tagenv, b1)["addr"] == 2  # the other fields survive the merge

    for bad in ({"max_tags": 0, "assigned": -1}, {"max_tags": 256, "assigned": 256},
                {"max_tags": "10", "assigned": 1.5}, {"max_tags": True, "assigned": None}):
        report(tagenv, hw, {"B1": {**bad, "fw": "0.2.0"}})
        assert info(tagenv, b1)["max_tags"] == 10 and info(tagenv, b1)["assigned"] == 3
    assert scalar(tagenv, "SELECT fw FROM tag_devices WHERE id=:i", i=b1) == "0.2.0"

    fresh = hardware(tagenv, tags=(), bridges=("B9",))
    report(tagenv, fresh, {"B9": {"max_tags": 300, "assigned": 4}})
    assert "max_tags" not in info(tagenv, fresh["bridges"]["B9"])
    assert info(tagenv, fresh["bridges"]["B9"])["assigned"] == 4


# ── (2) claim / assign refuse a full bridge ─────────────────────────────────


def test_claim_and_assign_refuse_a_full_bridge(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2", "T3", "T4"), bridges=("B1", "B2"))
    t = hw["tags"]
    b1, b2 = hw["bridges"]["B1"], hw["bridges"]["B2"]
    report(tagenv, hw, {"B1": {"max_tags": 2}})
    run(service.rename_device(b1, "Hall"))
    claim_to = lambda tag, owner, bridge: admin(  # noqa: E731
        CLAIM, "POST", path={"device_id": tag}, body={"owner": owner, "bridge_id": bridge})

    assert claim_to(t["T1"], "p1", b1).status_code == 200
    assert claim_to(t["T2"], "p2", b1).status_code == 200
    # A released tag keeps its slot: the bridge still holds it.
    run(service.release_tag(t["T2"], requested_by="admin"))
    commands_before = scalar(tagenv, "SELECT COUNT(*) FROM tag_commands")

    full = claim_to(t["T3"], "p1", b1)
    assert full.status_code == 409
    body = body_of(full)
    assert body["error"] == "bridge_full" and body["detail"] == body["message"]
    assert "2 of 2" in body["message"]
    assert body["bridge"] == {"id": b1, "name": "Hall", "max_tags": 2, "assigned": 2}
    moved = admin(ASSIGN, "POST", path={"device_id": t["T3"]}, body={"bridge_id": b1})
    assert moved.status_code == 409 and body_of(moved)["error"] == "bridge_full"
    assert device(tagenv, t["T3"])["epoch"] == 0 and device(tagenv, t["T3"])["owner_profile"] is None
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_commands") == commands_before

    # The tag being (re)assigned does not count against itself.
    assert claim_to(t["T1"], "p2", b1).status_code == 200
    assert admin(ASSIGN, "POST", path={"device_id": t["T1"]}, body={"bridge_id": b1}).status_code == 200
    # Unknown capacity allows.
    assert claim_to(t["T3"], "p1", b2).status_code == 200
    # Moving a tag off frees its slot; so does forgetting an unowned one.
    assert admin(ASSIGN, "POST", path={"device_id": t["T1"]}, body={"bridge_id": b2}).status_code == 200
    assert claim_to(t["T4"], "p1", b1).status_code == 200
    assert claim_to(t["T3"], "p1", b1).status_code == 409
    run(service.forget_device(t["T2"]))
    assert claim_to(t["T3"], "p1", b1).status_code == 200


def test_a_claim_without_bridge_id_is_checked_against_the_bridge_it_lands_on(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2"), bridges=("B1",))
    report(tagenv, hw, {"B1": {"max_tags": 1}})
    claim(tagenv, hw["tags"]["T1"], "p1")
    with pytest.raises(TagError) as exc:
        run(service.claim_tag(hw["tags"]["T2"], owner="p2", requested_by="admin"))
    assert exc.value.status == 409 and exc.value.code == "bridge_full"
    assert exc.value.extra["bridge"]["assigned"] == 1


def test_two_claims_cannot_both_take_the_last_slot(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2", "T3"), bridges=("B1",))
    b1 = hw["bridges"]["B1"]
    report(tagenv, hw, {"B1": {"max_tags": 2}})
    claim(tagenv, hw["tags"]["T1"], "p1")

    async def race():
        return await asyncio.gather(*(
            service.claim_tag(hw["tags"][t], owner=owner, bridge_id=b1, requested_by="admin")
            for t, owner in (("T2", "p1"), ("T3", "p2"))
        ), return_exceptions=True)

    results = run(race())
    refused = [r for r in results if isinstance(r, TagError)]
    assert len(refused) == 1 and refused[0].code == "bridge_full", results
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_devices WHERE bridge_device_id=:b", b=b1) == 2


def test_capacity_checks_do_not_deadlock_with_heartbeats_and_reports(tagenv) -> None:
    """Claims lock a tag, then its bridge; heartbeats, inventory reports and a
    bridge's forget take device rows in the same order."""
    tags = tuple(f"T{i}" for i in range(8))
    hw = hardware(tagenv, tags=tags, bridges=("B1", "B2"))
    b1, b2 = hw["bridges"]["B1"], hw["bridges"]["B2"]
    report(tagenv, hw, {"B1": {"max_tags": 20}, "B2": {"max_tags": 20}})
    beat = {"devices": [{"hw_id": "B1", "kind": "bridge", "status": "ok"},
                        {"hw_id": "B2", "kind": "bridge", "status": "ok"}]
            + [{"hw_id": t, "kind": "tag", "status": "ok"} for t in reversed(tags)]}
    inv = {"bridges": [{"hw_id": "B2", "max_tags": 20}, {"hw_id": "B1", "max_tags": 20}],
           "tags": [{"tag_id": t} for t in reversed(tags)]}

    async def storm():
        work = []
        for i, t in enumerate(tags):
            bridge = b1 if i % 2 else b2
            work.append(service.claim_tag(hw["tags"][t], owner="p1" if i % 3 else "p2", bridge_id=bridge,
                                          requested_by="admin"))
            work.append(tagenv.store.record_heartbeat(hw["companion_id"], beat))
            work.append(tagenv.store.upsert_inventory(hw["companion_id"], inv))
        return await asyncio.gather(*work, return_exceptions=True)

    for _ in range(3):
        failures = [r for r in run(storm()) if isinstance(r, BaseException)]
        assert failures == []


# ── (3) an assign_tag the bridge refused ────────────────────────────────────


def test_a_bridge_full_result_marks_the_tag_assign_failed(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",), bridges=("B1", "B2"))
    tag, b1, b2 = hw["tags"]["T1"], hw["bridges"]["B1"], hw["bridges"]["B2"]
    run(service.claim_tag(tag, owner="p1", bridge_id=b1, requested_by="admin"))
    cmd = assign_command(tagenv, "T1")
    resp = connector(RESULT, "POST", hw["auth"], path={"command_id": cmd["id"]},
                     body={"status": "failed", "result": {"error": "bridge_full", "max_tags": 10}})
    assert resp.status_code == 200, resp.body
    stored = body_of(admin("/api/tags/hardware/commands/{command_id}", "GET",
                           path={"command_id": cmd["id"]}))["command"]
    assert stored["status"] == "failed" and stored["error"] == "bridge_full"
    assert stored["result"] == {"error": "bridge_full", "max_tags": 10}

    d = device(tagenv, tag)
    assert d["status"] == "assign_failed" and d["bridge_device_id"] is None
    assert d["owner_profile"] == "p1"
    assert info(tagenv, b1)["max_tags"] == 10  # learned from the refusal
    # A heartbeat does not paper over it.
    run(tagenv.store.record_heartbeat(hw["companion_id"],
                                      {"devices": [{"hw_id": "T1", "kind": "tag", "status": "ok"}]}))
    assert device(tagenv, tag)["status"] == "assign_failed"
    inventory = body_of(admin("/api/tags/hardware", "GET"))
    bridge = next(x for x in inventory["devices"] if x["id"] == b1)
    assert bridge["max_tags"] == 10 and bridge["assigned_count"] == 0

    # Recovery: assign it to another bridge.
    moved = body_of(admin(ASSIGN, "POST", path={"device_id": tag}, body={"bridge_id": b2}))
    assert moved["device"]["status"] == "assigning" and moved["device"]["bridge_device_id"] == b2
    done = connector(RESULT, "POST", hw["auth"], path={"command_id": moved["command"]["id"]},
                     body={"status": "succeeded"})
    assert done.status_code == 200, done.body
    assert device(tagenv, tag)["status"] == "assigning"


def test_assign_failed_recovers_by_release_and_ignores_stale_results(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2"), bridges=("B1",))
    b1 = hw["bridges"]["B1"]
    run(service.claim_tag(hw["tags"]["T1"], owner="p1", bridge_id=b1, requested_by="admin"))
    first = assign_command(tagenv, "T1")
    # An explicit error wins over result.error; any failure marks the tag.
    run(tagenv.store.complete_command(hw["companion_id"], first["id"], status="failed",
                                      result={"error": "bridge_full"}, error="tag out of range"))
    d = device(tagenv, hw["tags"]["T1"])
    assert d["status"] == "assign_failed" and d["bridge_device_id"] == b1
    assert scalar(tagenv, "SELECT error FROM tag_commands WHERE id=:i", i=first["id"]) == "tag out of range"
    released = run(service.release_tag(hw["tags"]["T1"], requested_by="admin"))
    assert released["device"]["status"] == "unclaimed"

    # A late failure for an epoch the tag has moved past changes nothing.
    run(service.claim_tag(hw["tags"]["T2"], owner="p2", bridge_id=b1, requested_by="admin"))
    old = assign_command(tagenv, "T2")
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_commands SET status='expired' WHERE id=:i"), {"i": old["id"]})
    run(service.assign_tag(hw["tags"]["T2"], bridge_id=b1, requested_by="admin"))
    run(tagenv.store.complete_command(hw["companion_id"], old["id"], status="failed",
                                      result={"error": "bridge_full", "max_tags": 5}, error=None))
    assert device(tagenv, hw["tags"]["T2"])["status"] == "assigning"
    assert device(tagenv, hw["tags"]["T2"])["bridge_device_id"] == b1
    assert "max_tags" not in info(tagenv, b1)


# ── (4) the admin inventory ─────────────────────────────────────────────────


def test_hardware_inventory_shows_each_bridges_capacity_and_count(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2", "T3"), bridges=("B1", "B2"))
    b1, b2 = hw["bridges"]["B1"], hw["bridges"]["B2"]
    report(tagenv, hw, {"B1": {"max_tags": 10, "assigned": 7}})
    run(service.claim_tag(hw["tags"]["T1"], owner="p1", bridge_id=b1, requested_by="admin"))
    run(service.claim_tag(hw["tags"]["T2"], owner="p2", bridge_id=b1, requested_by="admin"))
    run(service.release_tag(hw["tags"]["T2"], requested_by="admin"))
    run(service.claim_tag(hw["tags"]["T3"], owner="p1", bridge_id=b2, requested_by="admin"))
    devices = {d["id"]: d for d in body_of(admin("/api/tags/hardware", "GET"))["devices"]}
    assert devices[b1]["max_tags"] == 10 and devices[b1]["assigned_count"] == 2
    assert devices[b1]["info"]["assigned"] == 7  # what the bridge itself reports
    assert devices[b2]["max_tags"] is None and devices[b2]["assigned_count"] == 1
    tag = devices[hw["tags"]["T1"]]
    assert "max_tags" not in tag and "assigned_count" not in tag
    denied = run(find_handler(get_tags_hardware_routes(), "/api/tags/hardware", "GET")(make_request("p1")))
    assert denied.status_code == 403
