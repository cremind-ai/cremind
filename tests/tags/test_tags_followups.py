"""Fixes found while wrapping the Tags API in the CLI (both backends).

1. Forgetting a tag a profile owns is refused (409 ``tag_owned``); a released
   tag can be forgotten, and the companion's reported ``epoch`` in
   ``inventory`` keeps a re-reported tag from restarting below the epoch the
   tag itself stores (owed work is re-queued above it).
2. Renames need a real name (1..128 once stripped); the profile rename checks
   ownership and kind before anything is written.
3. A pinned note's body keeps its paragraphs, redacted line by line.
4. ``display`` decides ``clear_pending`` inside the transaction that writes.
5. ``PATCH`` settings / admin defaults merge; ``PUT`` still replaces.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

pytest.importorskip("a2a")

from app.api.tag_connector import get_tag_connector_routes  # noqa: E402
from app.api.tags import get_tags_routes  # noqa: E402
from app.api.tags_hardware import get_tags_hardware_routes  # noqa: E402
from app.tags import service  # noqa: E402
from app.tags.sanitize import clean_multiline  # noqa: E402
from tests.tags._helpers import (  # noqa: E402
    body_of, claim, enable, find_handler, hardware, make_request, rows, run, scalar,
)

ROUTES = get_tags_routes()
V1 = "/api/tag-connector/v1"


def call(route_path: str, method: str, user: str | None = "p1", *, routes=ROUTES, **kw):
    return run(find_handler(routes, route_path, method)(make_request(user, **kw)))


def inventory(hw: dict, tags: list[dict]):
    handler = find_handler(get_tag_connector_routes(), f"{V1}/inventory", "POST")
    resp = run(handler(make_request(None, headers={"Authorization": hw["auth"]},
                                    body={"gateways": [], "bridges": [], "tags": tags})))
    assert resp.status_code == 200
    return body_of(resp)


def queued(env, hw_id: str) -> list[tuple[str, dict]]:
    out = []
    for r in rows(env, "SELECT kind, args FROM tag_commands WHERE status='queued' ORDER BY created_at"):
        args = json.loads(r["args"])
        if args.get("tag_id") == hw_id:
            out.append((r["kind"], args))
    return out


# ── 1. forget / epoch floor ─────────────────────────────────────────────────


def test_forgetting_an_owned_tag_is_refused_until_it_is_released(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    hw_routes = get_tags_hardware_routes()
    path = "/api/tags/hardware/devices/{device_id}"
    refused = call(path, "DELETE", "admin", routes=hw_routes, path={"device_id": tag})
    assert refused.status_code == 409
    body = body_of(refused)
    assert body["error"] == "tag_owned" and body["device"]["owner_profile"] == "p1"
    assert run(tagenv.store.get_device(tag)) is not None

    call("/api/tags/hardware/tags/{device_id}/release", "POST", "admin", routes=hw_routes,
         path={"device_id": tag})
    forgotten = body_of(call(path, "DELETE", "admin", routes=hw_routes, path={"device_id": tag}))
    assert forgotten["deleted"] is True and forgotten["last_epoch"] == 2
    assert run(tagenv.store.get_device(tag)) is None
    missing = call(path, "DELETE", "admin", routes=hw_routes, path={"device_id": tag})
    assert missing.status_code == 404
    bridge = body_of(call(path, "DELETE", "admin", routes=hw_routes, path={"device_id": hw["bridges"]["B1"]}))
    assert bridge["device"]["kind"] == "bridge"


def test_a_re_reported_tag_keeps_the_epoch_the_companion_reports(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    run(service.release_tag(tag, requested_by="admin"))
    run(service.forget_device(tag))
    # The companion knows the tag last saw epoch 2 (it assigned it, or read
    # CHALLENGE.stored_epoch from the tag).
    out = inventory(hw, [{"tag_id": "T1", "epoch": 2}])
    assert [a["epoch"] for a in out["assignments"]] == [2]
    new_id = next(d["id"] for d in out["devices"] if d["kind"] == "tag")
    result = run(service.claim_tag(new_id, owner="p2", requested_by="admin"))
    assert result["device"]["epoch"] == 3
    assert {c["args"]["epoch"] for c in result["commands"]} == {3}


def test_the_reported_epoch_only_ever_raises_and_bad_values_are_ignored(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    inventory(hw, [{"tag_id": "T1", "epoch": 4}])
    assert run(tagenv.store.get_device(tag))["epoch"] == 4
    for bad in (1, -3, True, "9", 2 ** 32, None, 4.5):
        inventory(hw, [{"tag_id": "T1", "epoch": bad}])
        assert run(tagenv.store.get_device(tag))["epoch"] == 4, bad
    assert queued(tagenv, "T1") == []  # an unowned, cleared tag owes nothing
    fresh = inventory(hw, [{"tag_id": "T9"}])
    assert next(d["epoch"] for d in fresh["devices"] if d["hw_id"] == "T9") == 0


def test_a_report_ahead_of_an_owned_tag_requeues_its_work(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2"))
    owned, pending = hw["tags"]["T1"], hw["tags"]["T2"]
    claim(tagenv, owned, "p1")                    # epoch 1, cleared
    claim(tagenv, pending, "p1", cleared=False)   # epoch 1, clear still owed
    enable(tagenv, "p1")
    note = run(service.display("p1", owned, {"title": "keep me"}))
    assert note["epoch"] == 1
    out = inventory(hw, [{"tag_id": "T1", "epoch": 5}, {"tag_id": "T2", "epoch": 7}])
    epochs = {a["tag_id"]: a["epoch"] for a in out["assignments"]}
    assert epochs == {"T1": 6, "T2": 8}
    assert queued(tagenv, "T1") == [("assign_tag", {"tag_id": "T1", "bridge_hw_id": "B1", "epoch": 6})]
    assert queued(tagenv, "T2") == [
        ("assign_tag", {"tag_id": "T2", "bridge_hw_id": "B1", "epoch": 8}),
        ("clear_tag", {"tag_id": "T2", "epoch": 8}),
    ]
    assert scalar(tagenv, "SELECT epoch FROM tag_deliveries WHERE id=:i", i=note["id"]) == 6
    assert run(tagenv.store.get_device(pending))["clear_required"] is True


def test_a_report_ahead_of_a_tag_still_pairing_leaves_its_epoch_to_the_operation(tagenv) -> None:
    # A pair or import operation assigned the tag at the worker (epoch 1) and
    # waits for its first screen. An inventory meanwhile (after a gateway
    # reboot) must not queue an assignment above it: that cancels its CLEAR.
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_devices SET owner_profile='p1', bridge_device_id=:b, status='pairing', "
                       "clear_required=:t WHERE id=:i"), {"b": hw["bridges"]["B1"], "t": True, "i": tag})
    inventory(hw, [{"tag_id": "T1", "epoch": 1}])
    assert queued(tagenv, "T1") == []
    device = run(tagenv.store.get_device(tag))
    assert (device["epoch"], device["status"], device["clear_required"]) == (0, "pairing", True)


# ── 2. renames ──────────────────────────────────────────────────────────────


def test_renames_need_a_real_name(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    path = "/api/tags/devices/{device_id}"
    hw_routes = get_tags_hardware_routes()
    hw_path = "/api/tags/hardware/devices/{device_id}"
    for bad in ("", "   ", "\n\t", "x" * 129, 42, None):
        for resp in (call(path, "PATCH", "p1", path={"device_id": tag}, body={"name": bad}),
                     call(hw_path, "PATCH", "admin", routes=hw_routes, path={"device_id": tag}, body={"name": bad})):
            assert resp.status_code == 422 and body_of(resp)["error"] == "invalid_name", repr(bad)
    assert run(tagenv.store.get_device(tag))["name"] == ""
    ok = body_of(call(path, "PATCH", "p1", path={"device_id": tag}, body={"name": "  Desk  "}))
    assert ok["device"]["name"] == "Desk"
    bridge = body_of(call(hw_path, "PATCH", "admin", routes=hw_routes,
                          path={"device_id": hw["bridges"]["B1"]}, body={"name": " Hall bridge "}))
    assert bridge["device"]["name"] == "Hall bridge"
    with pytest.raises(service.TagError) as exc:
        run(service.claim_tag(tag, owner="p2", name="   ", requested_by="admin"))
    assert exc.value.code == "invalid_name"


def test_the_profile_rename_checks_ownership_before_writing(tagenv, monkeypatch) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag, bridge = hw["tags"]["T1"], hw["bridges"]["B1"]
    claim(tagenv, tag, "p1")
    writes: list[str] = []
    real = tagenv.store.rename_device

    async def spy(device_id, name, **kw):
        writes.append(device_id)
        return await real(device_id, name, **kw)

    monkeypatch.setattr(tagenv.store, "rename_device", spy)
    path = "/api/tags/devices/{device_id}"
    for user, device, body in (("p2", tag, {"name": "stolen"}), ("p2", tag, {"name": ""}),
                               ("p1", bridge, {"name": "mine"})):
        resp = call(path, "PATCH", user, path={"device_id": device}, body=body)
        assert resp.status_code == 404 and body_of(resp)["error"] == "device_not_found"
    assert writes == []
    assert run(tagenv.store.get_device(bridge))["name"] == ""
    # The UPDATE is also scoped: a stale caller cannot rename a bridge.
    assert run(real(bridge, "x", owner="p1")) is None


# ── 3. multi-line pinned notes ──────────────────────────────────────────────


def test_clean_multiline_keeps_paragraphs() -> None:
    raw = "\n\nLine one   \r\n\r\n\n\n  indented   item\t\n\tsub\n\nlast  line   \n\n\n"
    assert clean_multiline(raw, 400) == "Line one\n\n  indented item\n  sub\n\nlast line"
    assert clean_multiline("wifi:\npassword=hunter2hunter2\nok", 400) == "wifi:\npassword=[redacted]\nok"
    assert clean_multiline("a\n" * 300, 20).endswith("…") and len(clean_multiline("a\n" * 300, 20)) <= 20


def test_display_keeps_the_bodys_paragraphs(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    path = "/api/tags/devices/{device_id}/display"
    resp = call(path, "POST", "p1", path={"device_id": tag}, body={
        "title": "Shopping\nlist", "body": "Milk  \n\n\n\nEggs\nkey=abcdefghijklmnop\n",
    })
    assert resp.status_code == 201
    card = body_of(resp)["delivery"]["card"]
    assert card["title"] == "Shopping list"
    assert card["body"] == "Milk\n\nEggs\nkey=[redacted]"
    otp = call(path, "POST", "p1", path={"device_id": tag},
               body={"title": "Login", "body": "Hello\n\nYour verification code is 482913"})
    assert otp.status_code == 422 and body_of(otp)["error"] == "otp_refused"


# ── 4. clear_pending decided in the writing transaction ─────────────────────


def test_display_rechecks_clear_required_inside_the_transaction(tagenv, monkeypatch) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1", cleared=False)
    real = service.owned_tag

    async def stale(profile, device_id):
        # The caller's first read happened before the claim: it saw no hold.
        return {**(await real(profile, device_id)), "clear_required": False}

    monkeypatch.setattr(service, "owned_tag", stale)
    before = scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries")
    with pytest.raises(service.TagError) as exc:
        run(service.display("p1", tag, {"title": "too early"}))
    assert exc.value.status == 409 and exc.value.code == "clear_pending"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries") == before
    # The clear itself is not content: it goes through.
    assert run(service.clear("p1", tag))["kind"] == "clear"


# ── 5. PATCH merges settings ────────────────────────────────────────────────


def _options(user: str) -> dict:
    return body_of(call("/api/tags/settings", "GET", user))["options"]


def test_patch_settings_merges_and_put_replaces(tagenv) -> None:
    put = call("/api/tags/settings", "PUT", "p1",
               body={"options": {"language": "vi", "routes": {"usage": "all"}}})
    assert put.status_code == 200
    patched = call("/api/tags/settings", "PATCH", "p1",
                   body={"options": {"show_excerpts": True, "routes": {"notification": "none"}}})
    assert patched.status_code == 200
    body = body_of(patched)
    assert body["options"] == {"language": "vi", "show_excerpts": True,
                               "routes": {"usage": "all", "notification": "none"}}
    assert body["effective"]["routes"]["usage"] == "all" and body["effective"]["show_excerpts"] is True
    call("/api/tags/settings", "PATCH", "p1", body={"options": {"language": None, "routes": {"usage": None}}})
    assert _options("p1") == {"show_excerpts": True, "routes": {"notification": "none"}}
    call("/api/tags/settings", "PATCH", "p1", body={"options": {"routes": None}})
    assert _options("p1") == {"show_excerpts": True}

    for bad in ({"options": {"progress_cadence_s": 5}}, {"options": {"routes": {"rockets": "all"}}},
                {"options": None}, {"options": [1]}, {"colour": "red"}, {"enabled": "yes"}):
        resp = call("/api/tags/settings", "PATCH", "p1", body=bad)
        assert resp.status_code == 422 and body_of(resp)["error"] == "invalid_settings", bad
    assert _options("p1") == {"show_excerpts": True}

    enabled = body_of(call("/api/tags/settings", "PATCH", "p1", body={"enabled": True}))
    assert enabled["enabled"] is True and enabled["options"] == {"show_excerpts": True}
    # A profile's first write may be a PATCH; nobody else's settings move.
    assert body_of(call("/api/tags/settings", "PATCH", "p2", body={"options": {"qr_links": True}}))[
        "options"] == {"qr_links": True}
    assert _options("p1") == {"show_excerpts": True}
    # PUT is still a full replace.
    call("/api/tags/settings", "PUT", "p1", body={"options": {"layout": "status"}})
    assert _options("p1") == {"layout": "status"}


def test_concurrent_patches_do_not_lose_each_others_keys(tagenv) -> None:
    kinds = ["notification", "task_outcome", "needs_input", "progress", "health", "calendar"]
    handler = find_handler(ROUTES, "/api/tags/settings", "PATCH")

    async def go():
        return await asyncio.gather(*(
            handler(make_request("p1", body={"options": {"routes": {k: "none"}}})) for k in kinds
        ))

    assert {r.status_code for r in run(go())} == {200}
    assert _options("p1") == {"routes": {k: "none" for k in kinds}}


def test_patch_admin_defaults(tagenv, monkeypatch) -> None:
    import app.config.settings as settings_mod
    from app.storage.dynamic_config_storage import DynamicConfigStorage

    cfg = DynamicConfigStorage(tagenv.provider)
    monkeypatch.setattr(settings_mod, "_dynamic_config_storage", cfg)
    routes = get_tags_hardware_routes(cfg)
    path = "/api/tags/hardware/defaults"
    assert call(path, "PUT", "admin", routes=routes,
                body={"defaults": {"language": "vi", "routes": {"usage": "all"}}}).status_code == 200
    merged = body_of(call(path, "PATCH", "admin", routes=routes,
                          body={"defaults": {"show_excerpts": True, "routes": {"progress": "none"}}}))
    assert merged["defaults"] == {"language": "vi", "show_excerpts": True,
                                  "routes": {"usage": "all", "progress": "none"}}
    removed = body_of(call(path, "PATCH", "admin", routes=routes,
                           body={"defaults": {"language": None, "routes": {"usage": None}}}))
    assert removed["defaults"] == {"show_excerpts": True, "routes": {"progress": "none"}}
    assert body_of(call(path, "GET", "admin", routes=routes))["defaults"] == removed["defaults"]
    # A profile inherits the merged defaults.
    assert body_of(call("/api/tags/settings", "GET", "p2"))["effective"]["show_excerpts"] is True
    for bad in ({"defaults": {"layout": "poster"}}, {"defaults": None}, {}):
        resp = call(path, "PATCH", "admin", routes=routes, body=bad)
        assert resp.status_code == 422, bad
    assert body_of(call(path, "GET", "admin", routes=routes))["defaults"] == removed["defaults"]
    assert call(path, "PATCH", "p1", routes=routes, body={"defaults": {}}).status_code == 403
    replaced = body_of(call(path, "PUT", "admin", routes=routes, body={"defaults": {"qr_links": True}}))
    assert replaced["defaults"] == {"qr_links": True}
