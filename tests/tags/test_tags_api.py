"""The profile API (``/api/tags``), the admin API (``/api/tags/hardware``) and
the Tags lifecycle.

- Strict two-profile isolation: for EVERY ``/api/tags`` route, ``p2`` can
  neither see nor act on ``p1``'s devices, deliveries, previews or credentials
  (404, never 403 — ids are not confirmed across profiles).
- ``/api/tags/hardware*`` is admin-only (403 for a profile, 401 anonymous).
- claim / assign / release bump the epoch, hold content (``clear_required``),
  cancel the old owner's work and queue ``assign_tag`` / ``clear_tag``.
- ``display`` sanitises and refuses a one-time code (422 ``otp_refused``).
- Deleting a profile releases its tags; a backup never carries connector
  credentials; the restore close-out cancels live deliveries, rotates stream
  ids, jumps the delivery counter and re-pins this install's credentials.
- ``app/server.py`` installs the guard between ``ClientProtocolGuard`` and
  ``AuthenticationMiddleware``, and starts/stops the projection worker.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.api.tags import get_tags_routes  # noqa: E402
from app.api.tags_hardware import get_tags_hardware_routes  # noqa: E402
from app.tags import journal  # noqa: E402
from app.tags.journal import JournalEntry  # noqa: E402
from app.tags.projection import TagProjectionWorker  # noqa: E402
from tests.tags._helpers import (  # noqa: E402
    body_of, claim, content_credential, enable, find_handler, hardware, make_request, rows, run, scalar,
)

ROUTES = get_tags_routes()
HW_ROUTES = get_tags_hardware_routes()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32


def call(route_path: str, method: str, user: str | None = "p1", *, routes=ROUTES, **kw):
    return run(find_handler(routes, route_path, method)(make_request(user, **kw)))


def _deliver(env, profile: str, n: int = 1) -> None:
    async def go():
        async with env.provider.async_engine().begin() as conn:
            await journal.append_async(conn, profile, [
                JournalEntry(kind="notification", payload={"kind": "x", "title": f"t{i}", "preview": "",
                                                           "priority": "normal"}, source_type="t")
                for i in range(n)
            ])
    run(go())
    run(TagProjectionWorker(env.store).project_profile(profile))


@pytest.fixture
def world(tagenv):
    hw = hardware(tagenv, tags=("T1", "T2"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    claim(tagenv, hw["tags"]["T2"], "p2")
    enable(tagenv, "p1")
    enable(tagenv, "p2")
    _deliver(tagenv, "p1", 2)
    _deliver(tagenv, "p2", 1)
    run(tagenv.store.store_preview(hw["companion_id"], "p1", "T1", kind="displayed", revision=1, epoch=None,
                                   png_base64=base64.b64encode(PNG).decode(), delivery_ids=[]))
    c1 = content_credential(tagenv, "p1", hw["companion_id"])
    hw["c1_id"] = c1.split(" ", 1)[1].split(".", 1)[0]
    hw["p1_delivery"] = rows(tagenv, "SELECT id FROM tag_deliveries WHERE profile='p1' ORDER BY id")[0]["id"]
    return hw


# ── isolation ───────────────────────────────────────────────────────────────


def test_every_profile_route_is_isolated(tagenv, world) -> None:
    t1 = world["tags"]["T1"]
    did = str(world["p1_delivery"])
    device_calls = [
        ("/api/tags/devices/{device_id}", "GET", {}),
        ("/api/tags/devices/{device_id}", "PATCH", {"body": {"name": "mine"}}),
        ("/api/tags/devices/{device_id}/display", "POST", {"body": {"title": "Hi"}}),
        ("/api/tags/devices/{device_id}/clear", "POST", {}),
        ("/api/tags/devices/{device_id}/refresh", "POST", {}),
        ("/api/tags/devices/{device_id}/identify", "POST", {}),
        ("/api/tags/devices/{device_id}/preview", "GET", {"query": {"kind": "displayed"}}),
    ]
    for route_path, method, kw in device_calls:
        resp = call(route_path, method, "p2", path={"device_id": t1}, **kw)
        assert resp.status_code == 404, (method, route_path)
        assert body_of(resp)["error"] == "device_not_found", (method, route_path)
        # ...while the owner reaches it.
        owner = call(route_path, method, "p1", path={"device_id": t1}, **kw)
        assert owner.status_code in (200, 201, 202), route_path

    assert call("/api/tags/deliveries/{delivery_id}", "GET", "p2", path={"delivery_id": did}).status_code == 404
    assert call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p2",
                path={"delivery_id": did}).status_code == 404
    assert call("/api/tags/deliveries", "GET", "p2", query={"device": t1}).status_code == 404
    p2_list = body_of(call("/api/tags/deliveries", "GET", "p2"))["deliveries"]
    assert p2_list and {d["profile"] for d in p2_list} == {"p2"}
    assert all(d["device_id"] != t1 for d in p2_list)

    overview = body_of(call("/api/tags", "GET", "p2"))
    assert [d["hw_id"] for d in overview["devices"]] == ["T2"]

    assert call("/api/tags/credentials/{credential_id}", "DELETE", "p2",
                path={"credential_id": world["c1_id"]}).status_code == 404
    assert body_of(call("/api/tags/credentials", "GET", "p2"))["credentials"] == []
    assert [c["id"] for c in body_of(call("/api/tags/credentials", "GET", "p1"))["credentials"]] == [world["c1_id"]]
    assert run(tagenv.store.get_credential(world["c1_id"]))["revoked_at"] is None

    # Settings are per profile.
    call("/api/tags/settings", "PUT", "p2", body={"options": {"language": "vi"}})
    assert body_of(call("/api/tags/settings", "GET", "p1"))["effective"]["language"] == "en"
    assert body_of(call("/api/tags/settings", "GET", "p2"))["effective"]["language"] == "vi"


def test_every_route_needs_a_session(tagenv, world) -> None:
    for route in ROUTES:
        params = {k: "x" for k in route.param_convertors}
        resp = run(route.endpoint(make_request(None, path=params, body={})))
        assert resp.status_code == 401, route.path


def test_hardware_routes_are_admin_only(tagenv, world) -> None:
    for route in HW_ROUTES:
        params = {k: "x" for k in route.param_convertors}
        for user, status in (("p1", 403), (None, 401)):
            resp = run(route.endpoint(make_request(user, path=params, body={})))
            assert resp.status_code == status, (route.path, user)
    inv = body_of(call("/api/tags/hardware", "GET", "admin", routes=HW_ROUTES))
    assert {d["hw_id"] for d in inv["devices"]} >= {"T1", "T2", "B1", "gw-1"}
    assert inv["companions"][0]["credentials"][0]["kind"] == "hardware"
    assert "secret_sha256" not in json.dumps(inv)


# ── profile actions ─────────────────────────────────────────────────────────


def test_display_sanitises_and_refuses_codes(tagenv, world) -> None:
    t1 = world["tags"]["T1"]
    display_path = "/api/tags/devices/{device_id}/display"
    refused = call(display_path, "POST", "p1", path={"device_id": t1},
                   body={"title": "Login", "body": "Your verification code is 482913"})
    assert refused.status_code == 422 and body_of(refused)["error"] == "otp_refused"
    before = scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries")
    ok = call(display_path, "POST", "p1", path={"device_id": t1},
              body={"title": "Wifi", "body": "password=hunter2hunter2", "icon": "info", "ttl_s": 600})
    assert ok.status_code == 201
    delivery = body_of(ok)["delivery"]
    assert delivery["kind"] == "pinned_note" and "hunter2" not in json.dumps(delivery["card"])
    assert delivery["expires_at"] - delivery["created_at"] == pytest.approx(600_000, abs=5)
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries") == before + 1
    bad = call(display_path, "POST", "p1", path={"device_id": t1}, body={"title": "x", "icon": "rocket"})
    assert bad.status_code == 422 and body_of(bad)["error"] == "invalid_icon"
    # Each note is its own card (its own key); only ``replace: true`` notes
    # share the tag's one replaceable slot.
    call(display_path, "POST", "p1", path={"device_id": t1}, body={"title": "Newer"})
    notes = rows(tagenv, "SELECT stage, replace_key FROM tag_deliveries WHERE kind='pinned_note' ORDER BY id")
    assert [n["stage"] for n in notes] == ["queued", "queued"]
    assert all(n["replace_key"].startswith("delivery:") for n in notes)
    for title in ("slot 1", "slot 2"):
        call(display_path, "POST", "p1", path={"device_id": t1}, body={"title": title, "replace": True})
    notes = rows(tagenv, "SELECT stage, replace_key FROM tag_deliveries WHERE kind='pinned_note' ORDER BY id")
    assert [n["stage"] for n in notes] == ["queued", "queued", "superseded", "queued"]
    assert notes[3]["replace_key"] == f"pinned:{t1}"
    bad = call(display_path, "POST", "p1", path={"device_id": t1}, body={"title": "x", "replace": "yes"})
    assert bad.status_code == 422 and body_of(bad)["error"] == "invalid_replace"


def test_clear_cancels_the_tags_active_cards(tagenv, world) -> None:
    t1 = world["tags"]["T1"]
    resp = call("/api/tags/devices/{device_id}/clear", "POST", "p1", path={"device_id": t1})
    assert resp.status_code == 201 and body_of(resp)["delivery"]["kind"] == "clear"
    live = rows(tagenv, "SELECT kind, stage FROM tag_deliveries WHERE tag_device_id=:d AND stage='queued'", d=t1)
    assert live == [{"kind": "clear", "stage": "queued"}]
    # p2's deliveries are untouched.
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries WHERE profile='p2' AND stage='queued'") == 1


def test_deliveries_listing_and_cancel(tagenv, world) -> None:
    listed = body_of(call("/api/tags/deliveries", "GET", "p1", query={"limit": "1"}))
    assert len(listed["deliveries"]) == 1 and listed["next_before"] == listed["deliveries"][0]["id"]
    older = body_of(call("/api/tags/deliveries", "GET", "p1",
                         query={"before": str(listed["next_before"]), "state": "active"}))
    assert [d["id"] for d in older["deliveries"]] == [world["p1_delivery"]]
    did = str(world["p1_delivery"])
    cancelled = call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p1", path={"delivery_id": did})
    assert body_of(cancelled)["delivery"]["stage"] == "cancelled"
    again = call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p1", path={"delivery_id": did})
    assert again.status_code == 409 and body_of(again)["error"] == "already_terminal"
    assert call("/api/tags/deliveries", "GET", "p1", query={"state": "bogus"}).status_code == 400


def test_settings_validation_and_admin_defaults(tagenv, world, monkeypatch) -> None:
    bad = call("/api/tags/settings", "PUT", "p1", body={"options": {"routes": {"rockets": "all"}, "x": 1}})
    assert bad.status_code == 422 and set(body_of(bad)["details"]) == {"routes.rockets", "x"}
    store: dict = {}

    class Cfg:
        def set(self, table, key, value, **kw):
            store[key] = value

    import app.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_dynamic",
                        lambda table, key, default=None, profile=None: store.get(key, default))
    resp = run(find_handler(get_tags_hardware_routes(Cfg()), "/api/tags/hardware/defaults", "PUT")(
        make_request("admin", body={"defaults": {"show_excerpts": True, "language": "vi"}})))
    assert resp.status_code == 200
    p2 = body_of(call("/api/tags/settings", "GET", "p2"))
    assert p2["defaults"]["show_excerpts"] is True and p2["effective"]["show_excerpts"] is True
    call("/api/tags/settings", "PUT", "p2", body={"options": {"show_excerpts": False}})
    p2 = body_of(call("/api/tags/settings", "GET", "p2"))
    assert p2["effective"]["show_excerpts"] is False and p2["effective"]["language"] == "vi"


def test_credentials_are_shown_once_and_revocable(tagenv, world) -> None:
    created = call("/api/tags/credentials", "POST", "p2",
                   body={"companion_id": world["companion_id"], "label": "laptop"})
    assert created.status_code == 201
    body = body_of(created)
    assert body["authorization"] == f"CremindTag {body['credential']['id']}.{body['secret']}"
    assert body["credential"]["profile"] == "p2" and "secret_sha256" not in body["credential"]
    listed = body_of(call("/api/tags/credentials", "GET", "p2"))["credentials"]
    assert "secret" not in json.dumps(listed).replace("secret_", "")
    revoked = call("/api/tags/credentials/{credential_id}", "DELETE", "p2",
                   path={"credential_id": body["credential"]["id"]})
    assert body_of(revoked)["credential"]["revoked"] is True
    missing = call("/api/tags/credentials", "POST", "p2", body={"companion_id": "nope"})
    assert missing.status_code == 404


# ── admin: ownership ────────────────────────────────────────────────────────


def test_claim_assign_release_flow(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",), bridges=("B1", "B2"))
    tag = hw["tags"]["T1"]
    claim_path = "/api/tags/hardware/tags/{device_id}/claim"
    need_bridge = call(claim_path, "POST", "admin", routes=HW_ROUTES, path={"device_id": tag}, body={"owner": "p1"})
    assert need_bridge.status_code == 409 and body_of(need_bridge)["error"] == "bridge_required"
    nobody = call(claim_path, "POST", "admin", routes=HW_ROUTES, path={"device_id": tag},
                  body={"owner": "ghost", "bridge_id": hw["bridges"]["B1"]})
    assert nobody.status_code == 422 and body_of(nobody)["error"] == "unknown_profile"

    first = body_of(call(claim_path, "POST", "admin", routes=HW_ROUTES, path={"device_id": tag},
                         body={"owner": "p1", "bridge_id": hw["bridges"]["B1"], "name": "Desk"}))
    assert first["device"]["epoch"] == 1 and first["device"]["clear_required"] is True
    assert first["device"]["owner_profile"] == "p1" and first["device"]["name"] == "Desk"
    assert [(c["kind"], c["args"]) for c in first["commands"]] == [
        ("assign_tag", {"tag_id": "T1", "bridge_hw_id": "B1", "epoch": 1}),
        ("clear_tag", {"tag_id": "T1", "epoch": 1}),
    ]
    run(tagenv.store.complete_command(hw["companion_id"], first["commands"][1]["id"],
                                      status="succeeded", result=None, error=None))
    enable(tagenv, "p1")
    _deliver(tagenv, "p1", 1)

    second = body_of(call(claim_path, "POST", "admin", routes=HW_ROUTES, path={"device_id": tag},
                          body={"owner": "p2"}))
    assert second["device"]["epoch"] == 2 and second["device"]["owner_profile"] == "p2"
    assert scalar(tagenv, "SELECT stage FROM tag_deliveries WHERE profile='p1'") == "cancelled"
    assert scalar(tagenv, "SELECT status FROM tag_commands WHERE id=:i", i=first["commands"][0]["id"]) == "cancelled"
    assert body_of(call("/api/tags", "GET", "p1"))["devices"] == []

    moved = body_of(call("/api/tags/hardware/tags/{device_id}/assign", "POST", "admin", routes=HW_ROUTES,
                         path={"device_id": tag}, body={"bridge_id": hw["bridges"]["B2"]}))
    assert moved["device"]["epoch"] == 3 and moved["command"]["args"]["bridge_hw_id"] == "B2"

    released = body_of(call("/api/tags/hardware/tags/{device_id}/release", "POST", "admin",
                            routes=HW_ROUTES, path={"device_id": tag}))
    assert released["device"]["owner_profile"] is None and released["device"]["epoch"] == 4
    assert [c["kind"] for c in released["commands"]] == ["clear_tag"]


def test_admin_commands_are_bounded(tagenv) -> None:
    hw = hardware(tagenv, tags=(), bridges=())
    ok = call("/api/tags/hardware/commands", "POST", "admin", routes=HW_ROUTES,
              body={"companion_id": hw["companion_id"], "kind": "scan_unprovisioned", "args": {"duration_s": 30}})
    assert ok.status_code == 202
    cmd = body_of(ok)["command"]
    got = call("/api/tags/hardware/commands/{command_id}", "GET", "admin", routes=HW_ROUTES,
               path={"command_id": cmd["id"]})
    assert body_of(got)["command"]["status"] == "queued"
    for kind, args, code in (("clear_tag", {}, "use_tag_endpoint"), ("format_disk", {}, "unknown_command"),
                             ("scan_unprovisioned", {"duration_s": 9999}, "invalid_args"),
                             ("identify", {}, "invalid_args")):
        resp = call("/api/tags/hardware/commands", "POST", "admin", routes=HW_ROUTES,
                    body={"companion_id": hw["companion_id"], "kind": kind, "args": args})
        assert resp.status_code == 422 and body_of(resp)["error"] == code, kind


def test_register_rotate_and_delete_companion(tagenv) -> None:
    reg = call("/api/tags/hardware/companions", "POST", "admin", routes=HW_ROUTES, body={"name": "Office"})
    assert reg.status_code == 201
    body = body_of(reg)
    cid = body["companion"]["id"]
    assert body["credential"]["kind"] == "hardware" and body["authorization"].startswith("CremindTag tagc_")
    rot = body_of(call("/api/tags/hardware/companions/{companion_id}/rotate", "POST", "admin",
                       routes=HW_ROUTES, path={"companion_id": cid}))
    assert rot["revoked"] == [body["credential"]["id"]]
    gone = call("/api/tags/hardware/companions/{companion_id}", "DELETE", "admin", routes=HW_ROUTES,
                path={"companion_id": cid})
    assert body_of(gone) == {"deleted": True}
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_credentials WHERE companion_id=:c", c=cid) == 0


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_deleting_a_profile_releases_its_tags(tagenv) -> None:
    from app.storage.conversation_storage import ConversationStorage

    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    run(cs.create_profile("p3"))
    hw = hardware(tagenv, tags=("T1", "T2"))
    claim(tagenv, hw["tags"]["T1"], "p3")
    claim(tagenv, hw["tags"]["T2"], "p1")
    enable(tagenv, "p3")
    _deliver(tagenv, "p3", 1)
    cred = content_credential(tagenv, "p3", hw["companion_id"]).split(" ", 1)[1].split(".", 1)[0]
    assert run(cs.delete_profile("p3")) is True
    dev = run(tagenv.store.get_device(hw["tags"]["T1"]))
    assert dev["owner_profile"] is None and dev["epoch"] == 2 and dev["clear_required"] is True
    assert dev["status"] == "unclaimed"
    clears = rows(tagenv, "SELECT args FROM tag_commands WHERE kind='clear_tag' AND status='queued'")
    assert [json.loads(c["args"]) for c in clears] == [{"tag_id": "T1", "epoch": 2}]
    assert run(tagenv.store.get_credential(cred)) is None
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries WHERE profile='p3'") == 0
    # The other profile's tag is untouched.
    assert run(tagenv.store.get_device(hw["tags"]["T2"]))["owner_profile"] == "p1"


def test_backups_never_carry_connector_credentials(tagenv, world) -> None:
    from app.backup.dbdump import dump_logical

    buf = io.BytesIO()
    stats = dump_logical(tagenv.engine, buf)
    assert "tag_credentials" not in stats.row_counts and "tag_counters" not in stats.row_counts
    import gzip

    raw = gzip.decompress(buf.getvalue()).decode()
    assert "secret_sha256" not in raw and '"table": "tag_credentials"' not in raw
    assert stats.row_counts["tag_devices"] >= 4


def test_restore_close_out(tagenv, world, tmp_path, monkeypatch) -> None:
    """Dump this install, load it into a fresh database (what a restore does),
    then run the close-out with the credentials captured before the wipe."""
    from app.backup.dbdump import dump_logical, load_logical
    from app.backup.engine import _capture_tag_credentials, _close_out_tags
    from app.databases.sqlite import SqliteDatabaseProvider
    import app.databases as dbs
    import app.storage.migrations as mig

    captured = _capture_tag_credentials(tagenv.engine)
    assert {c["kind"] for c in captured} == {"hardware", "content"}
    streams_before = {r["profile"]: r["stream_id"] for r in rows(tagenv, "SELECT profile, stream_id FROM tag_streams")}
    top_id = scalar(tagenv, "SELECT MAX(id) FROM tag_deliveries")
    buf = io.BytesIO()
    dump_logical(tagenv.engine, buf)

    target = SqliteDatabaseProvider(str(tmp_path / "restored.db"))
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: target)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: target)
    mig.upgrade("head")
    buf.seek(0)
    load_logical(target.sync_engine(), buf)
    report = _close_out_tags(target.sync_engine(), captured)
    eng = target.sync_engine()
    with eng.connect() as c:
        stages = {r[0] for r in c.execute(text("SELECT stage FROM tag_deliveries")).all()}
        details = {r[0] for r in c.execute(text("SELECT detail FROM tag_deliveries")).all()}
        counter = c.execute(text("SELECT value FROM tag_counters WHERE name='delivery_id'")).scalar()
        streams = {r[0]: r[1] for r in c.execute(text("SELECT profile, stream_id FROM tag_streams")).all()}
        creds = {r[0] for r in c.execute(text("SELECT id FROM tag_credentials")).all()}
    assert stages == {"cancelled"} and details == {"restored"}
    assert report["cancelled"] == 3
    assert counter == top_id + 2 ** 32
    assert set(streams) == set(streams_before)
    assert all(streams[p] != streams_before[p] for p in streams)
    assert creds == {c["id"] for c in captured}
    eng.dispose()


# ── server wiring ───────────────────────────────────────────────────────────


def test_the_guard_sits_between_the_protocol_guard_and_auth() -> None:
    import inspect

    import app.server as server

    source = inspect.getsource(server)
    stack = source[source.index("middleware_stack = ["):]
    stack = stack[:stack.index("]\n")]
    assert (stack.index("Middleware(ClientProtocolGuard)")
            < stack.index("Middleware(TagConnectorGuard)")
            < stack.index("AuthenticationMiddleware"))


def test_the_projection_worker_starts_after_the_boot_sweep_and_stops_on_shutdown() -> None:
    from pathlib import Path

    import app.server as server

    src = Path(server.__file__).read_text(encoding="utf-8")
    boot = src[src.index("async def boot_storage_and_post_storage"):]
    assert boot.index("await sweep_undelivered()") < boot.index("get_tag_projection_worker().start(loop)")
    shutdown = src[src.index("async def _do_shutdown"):src.index("async def _on_shutdown")]
    assert "get_tag_projection_worker().stop()" in shutdown


def test_the_jwt_backend_never_authenticates_the_tag_scheme(tagenv, monkeypatch) -> None:
    from types import SimpleNamespace

    import app.config.settings as settings_mod
    from app.server import JWTAuthBackend

    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: "s" * 40))
    hw = hardware(tagenv, tags=(), bridges=())
    conn = SimpleNamespace(headers={"Authorization": hw["auth"]}, url=SimpleNamespace(path="/api/tags"))
    backend = JWTAuthBackend(secret_provider=settings_mod.BaseConfig.get_jwt_secret)
    assert run(backend.authenticate(conn)) is None
