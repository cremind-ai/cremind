"""The 19 findings of the two-lens backend review, and the UI/daemon API asks.

Each test names the finding it pins. Everything runs on SQLite and — through
the ``tagenv`` fixture — on PostgreSQL when a throwaway database is set; the
two interleavings that only PostgreSQL READ COMMITTED can produce (a stage
changing between a read and its write) are PostgreSQL-only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from datetime import timedelta, timezone as fixed_zone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.api.tag_connector import get_tag_connector_routes  # noqa: E402
from app.api.tags import get_tags_routes  # noqa: E402
from app.tags import journal, routing, sanitize, service  # noqa: E402
from app.tags import storage as tag_storage  # noqa: E402
from app.tags.cards import cards_for_event  # noqa: E402
from app.tags.journal import JournalEntry, TurnContext  # noqa: E402
from app.tags.projection import TagProjectionWorker  # noqa: E402
from tests.tags._helpers import (  # noqa: E402
    body_of, claim, enable, find_handler, hardware, make_request, rows, run, scalar,
)

ROUTES = get_tags_routes()
V1 = "/api/tag-connector/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 40


def call(route_path, method, user="p1", *, routes=ROUTES, **kw):
    return run(find_handler(routes, route_path, method)(make_request(user, **kw)))


def connector(route_path, method, auth, **kw):
    handler = find_handler(get_tag_connector_routes(), f"{V1}/{route_path}", method)
    return run(handler(make_request(None, headers={"Authorization": auth}, **kw)))


def _append(env, profile, entries):
    async def go():
        async with env.provider.async_engine().begin() as conn:
            await journal.append_async(conn, profile, entries)
    run(go())


def _note(title):
    return JournalEntry(kind="notification", payload={"kind": "x", "title": title, "preview": "",
                                                      "priority": "normal"}, source_type="t")


def _deliveries(env, profile="p1"):
    return rows(env, "SELECT d.id, d.kind, d.stage, d.detail, d.epoch, d.replace_key, d.resolves, d.card "
                     "FROM tag_deliveries d WHERE d.profile = :p ORDER BY d.seq", p=profile)


def _preview(env, hw, profile, *, revision, epoch=None):
    return run(env.store.store_preview(hw["companion_id"], profile, "T1", kind="displayed",
                                       revision=revision, epoch=epoch,
                                       png_base64=base64.b64encode(PNG).decode(), delivery_ids=[]))


def _content(env, profile, hw):
    from tests.tags._helpers import content_credential

    return content_credential(env, profile, hw["companion_id"])


# ── [high] owner-specific state never carries over to a new owner ────────────


@pytest.mark.parametrize("change", ["release_claim", "delete_profile"])
def test_previews_name_and_revisions_do_not_survive_a_change_of_owner(tagenv, change) -> None:
    from app.storage.conversation_storage import ConversationStorage

    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    run(cs.create_profile("p3"))
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p3")
    run(service.rename_owned_tag("p3", tag, "p3's private tag"))
    assert _preview(tagenv, hw, "p3", revision=1_000_000) == (True, None)
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_devices SET desired_revision=1000000, displayed_revision=999999, "
                       "displayed_digest='deadbeef' WHERE id=:i"), {"i": tag})
    if change == "release_claim":
        run(service.release_tag(tag, requested_by="admin"))
    else:
        assert run(cs.delete_profile("p3")) is True
    run(service.claim_tag(tag, owner="p2", requested_by="admin"))
    device = run(tagenv.store.get_device(tag))
    assert (device["name"], device["desired_revision"], device["displayed_revision"],
            device["displayed_digest"]) == ("", 0, 0, None)
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_previews") == 0
    resp = call("/api/tags/devices/{device_id}/preview", "GET", "p2", path={"device_id": tag},
                query={"kind": "displayed"})
    assert resp.status_code == 404 and body_of(resp)["error"] == "no_preview"
    overview = body_of(call("/api/tags", "GET", "p2"))
    assert overview["devices"][0]["previews"] == {"desired": None, "displayed": None}
    # The new owner's own first preview is stored; the old owner's is refused.
    assert _preview(tagenv, hw, "p2", revision=1) == (True, None)
    if change == "release_claim":
        assert _preview(tagenv, hw, "p3", revision=5) == (False, "tag_not_found")


def test_previews_compare_revisions_within_one_epoch(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")                                     # epoch 1
    assert _preview(tagenv, hw, "p1", revision=50) == (True, None)
    assert _preview(tagenv, hw, "p1", revision=40) == (False, None)   # older, same epoch
    run(service.assign_tag(tag, bridge_id=hw["bridges"]["B1"], requested_by="admin"))  # epoch 2
    assert _preview(tagenv, hw, "p1", revision=1, epoch=1) == (False, "epoch_mismatch")
    assert _preview(tagenv, hw, "p1", revision=1, epoch=2) == (True, None)   # a new epoch replaces
    assert scalar(tagenv, "SELECT revision FROM tag_previews") == 1
    auth = _content(tagenv, "p1", hw)
    resp = connector("previews", "POST", auth, body={
        "tag_id": "T1", "revision": 2, "kind": "displayed", "epoch": 1,
        "png_base64": base64.b64encode(PNG).decode(), "delivery_ids": []})
    assert resp.status_code == 409 and body_of(resp)["error"] == "epoch_mismatch"


# ── [medium] ownership TOCTOU ────────────────────────────────────────────────


def _paused(monkeypatch, module, name):
    """Replace ``module.name`` by a coroutine that waits for ``release``."""
    real = getattr(module, name)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*a, **k):
        monkeypatch.setattr(module, name, real)
        entered.set()
        await release.wait()
        return await real(*a, **k)

    monkeypatch.setattr(module, name, paused)
    return entered, release


def test_a_claim_waits_for_an_in_flight_display_and_cancels_its_card(tagenv, monkeypatch) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")

    async def go():
        entered, release = _paused(monkeypatch, service, "write_deliveries")
        shown = asyncio.create_task(service.display("p1", tag, {"title": "racing"}))
        await asyncio.wait_for(entered.wait(), 5)
        moved = asyncio.create_task(service.claim_tag(tag, owner="p2", requested_by="admin"))
        await asyncio.sleep(0.4)
        blocked = not moved.done()
        release.set()
        return blocked, await shown, await moved

    blocked, note, moved = run(go())
    assert blocked, "the claim did not wait for the display's stream lock"
    assert moved["device"]["owner_profile"] == "p2" and moved["device"]["epoch"] == 2
    row = rows(tagenv, "SELECT stage, detail, epoch FROM tag_deliveries WHERE id=:i", i=note["id"])[0]
    assert (row["stage"], row["detail"], row["epoch"]) == ("cancelled", "reassigned", 1)


def test_a_claim_waits_for_an_in_flight_projection_batch(tagenv, monkeypatch) -> None:
    import app.tags.projection as projection_mod

    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    enable(tagenv, "p1")
    _append(tagenv, "p1", [_note("racing")])
    worker = TagProjectionWorker(tagenv.store)

    async def go():
        entered, release = _paused(monkeypatch, projection_mod, "write_deliveries")
        batch = asyncio.create_task(worker.project_profile("p1"))
        await asyncio.wait_for(entered.wait(), 5)
        moved = asyncio.create_task(service.claim_tag(tag, owner="p2", requested_by="admin"))
        await asyncio.sleep(0.4)
        blocked = not moved.done()
        release.set()
        await batch
        return blocked, await moved

    blocked, moved = run(go())
    assert blocked
    assert [(d["stage"], d["detail"]) for d in _deliveries(tagenv)] == [("cancelled", "reassigned")]


def test_connector_reads_are_defensive_about_owner_and_epoch(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    first = run(service.display("p1", tag, {"title": "one"}))
    second = run(service.display("p1", tag, {"title": "two"}))
    auth = _content(tagenv, "p1", hw)
    # A live card left at an older epoch (a race the locks now prevent).
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_deliveries SET epoch=0 WHERE id=:i"), {"i": first["id"]})
    jobs = body_of(connector("events", "GET", auth, query={"after": "0"}))["jobs"]
    assert [j["delivery_id"] for j in jobs] == [second["id"]]
    assert body_of(connector("accepted", "POST", auth,
                             body={"delivery_ids": [first["id"], second["id"]]}))["accepted"] == 1
    receipts = body_of(connector("receipts", "POST", auth, body={"receipts": [
        {"delivery_id": first["id"], "stage": "transferring"}]}))
    assert receipts["rejected"] == [{"delivery_id": first["id"], "reason": "epoch_mismatch"}]
    # The tag changed hands behind the connector's back.
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_devices SET owner_profile='p2' WHERE id=:i"), {"i": tag})
    page = body_of(connector("events", "GET", auth, query={"after": "0"}))
    assert page["jobs"] == [] and page["next_after"] == page["head_seq"] == 2
    receipts = body_of(connector("receipts", "POST", auth, body={"receipts": [
        {"delivery_id": second["id"], "stage": "transferring"}]}))
    assert receipts["rejected"] == [{"delivery_id": second["id"], "reason": "not_owned"}]


# ── [medium] clear_tag that expires or fails ────────────────────────────────


def test_an_expired_clear_is_requeued_and_a_late_success_still_lifts_the_hold(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    result = claim(tagenv, tag, "p1", cleared=False)
    clear_cmd = next(c for c in result["commands"] if c["kind"] == "clear_tag")
    store = tagenv.store
    run(store.claim_command(hw["companion_id"], clear_cmd["id"]))
    now = time.time() * 1000
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_commands SET expires_at=:t WHERE id=:i"), {"t": now - 1000, "i": clear_cmd["id"]})
    run(store.expire_commands())
    assert scalar(tagenv, "SELECT status FROM tag_commands WHERE id=:i", i=clear_cmd["id"]) == "claimed"
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_commands SET expires_at=:t WHERE id=:i"),
                  {"t": now - tag_storage.CLAIMED_GRACE_MS - 1000, "i": clear_cmd["id"]})
    run(store.expire_commands())
    assert scalar(tagenv, "SELECT status FROM tag_commands WHERE id=:i", i=clear_cmd["id"]) == "expired"
    requeued = rows(tagenv, "SELECT args FROM tag_commands WHERE kind='clear_tag' AND status='queued'")
    assert [json.loads(r["args"]) for r in requeued] == [{"tag_id": "T1", "epoch": 1}]
    row, problem = run(store.complete_command(hw["companion_id"], clear_cmd["id"], status="succeeded",
                                              result=None, error=None))
    assert problem is None and row["status"] == "succeeded"
    assert run(store.get_device(tag))["clear_required"] is False


def test_a_failing_clear_is_retried_then_marked_clear_failed(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1", cleared=False)
    store = tagenv.store
    for _ in range(tag_storage.CLEAR_RETRIES):
        queued = rows(tagenv, "SELECT id FROM tag_commands WHERE kind='clear_tag' AND status='queued'")
        assert len(queued) == 1
        run(store.complete_command(hw["companion_id"], queued[0]["id"], status="failed",
                                   result=None, error="tag asleep"))
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_commands WHERE kind='clear_tag' AND status='queued'") == 0
    assert run(store.get_device(tag))["status"] == "clear_failed"
    run(store.record_heartbeat(hw["companion_id"], {"devices": [{"hw_id": "T1", "kind": "tag", "status": "ok"}]}))
    assert run(store.get_device(tag))["status"] == "clear_failed"
    # A fresh claim starts over.
    again = run(service.claim_tag(tag, owner="p1", requested_by="admin"))
    assert again["device"]["status"] == "assigning"


# ── [medium] content held while the tag waits for its clear ─────────────────


def test_content_is_held_during_the_clear_and_delivered_when_it_lifts(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    enable(tagenv, "p1")
    ask = sanitize.turn_entries(TurnContext(profile="p1", conversation_title="Deploy"), conversation_id="c1",
                                message_id="m", role="agent", content="?",
                                metadata={"plan_mode": {"stage": "awaiting_approval", "plan": {"title": "Ship"}}})
    _append(tagenv, "p1", [e for e in ask if e.kind == "chat.needs_input"] + [_note("before the claim")])
    time.sleep(0.01)
    result = claim(tagenv, tag, "p1", cleared=False)
    time.sleep(0.01)
    _append(tagenv, "p1", sanitize.run_entries(run_id="r1", label="Backup", prior_status="running",
                                               new_status="pending", pending_question="Overwrite?")
            + sanitize.run_entries(run_id="r2", label="Sync", prior_status="running",
                                   new_status="pending", pending_question="Retry?")
            + sanitize.run_entries(run_id="r2", label="Sync", prior_status="pending", new_status="running")
            + [_note("during the clear")])
    run(TagProjectionWorker(tagenv.store).project_profile("p1"))
    assert _deliveries(tagenv) == []
    clear_cmd = next(c for c in result["commands"] if c["kind"] == "clear_tag")
    run(tagenv.store.complete_command(hw["companion_id"], clear_cmd["id"], status="succeeded",
                                      result=None, error=None))
    got = [(d["kind"], json.loads(d["card"])["title"]) for d in _deliveries(tagenv)]
    assert ("needs_input", "Approve plan: Ship") in got
    assert ("needs_input", "Overwrite?") in got
    assert ("notification", "during the clear") in got
    assert ("notification", "before the claim") not in got
    assert all(title != "Retry?" for _, title in got)


# ── [medium] periodic and diagnostics cards are refreshed while true ────────


def test_standing_periodic_content_and_diagnostics_are_refreshed(tagenv, monkeypatch) -> None:
    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p1")
    enable(tagenv, "p1", routes={"calendar": "none", "automation": "none", "usage": "none",
                                 "indexing_problem": "none"})
    journal.configure_standalone(tagenv.provider)
    worker = TagProjectionWorker(tagenv.store)

    async def health(profile, options):
        return {"title": "1 channel problem(s)", "body": "telegram: stopped", "count": 1}

    monkeypatch.setattr(worker, "_health", health)
    kinds = lambda k: scalar(tagenv, "SELECT COUNT(*) FROM tag_events WHERE kind=:k", k=k)  # noqa: E731
    run(worker.periodic_profile("p1"))
    run(worker.periodic_profile("p1"))
    assert kinds("health.summary") == 1

    def age(section, hours):
        async def go():
            async with tagenv.provider.async_engine().begin() as conn:
                await tag_storage.merge_stream_state(conn, "p1", lambda st: [
                    v.update(at=time.time() * 1000 - hours * 3_600_000) for v in st[section].values()])
        run(go())

    age("periodic", 13)
    run(worker.periodic_profile("p1"))
    assert kinds("health.summary") == 2
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_devices SET battery_mv=2000"))
    run(worker.diagnostics("p1", time.time() * 1000))
    run(worker.diagnostics("p1", time.time() * 1000))
    assert kinds("tag.diagnostics") == 1
    age("diag", 13)
    run(worker.diagnostics("p1", time.time() * 1000))
    assert kinds("tag.diagnostics") == 2
    # A pre-timestamp hash (a plain string) is refreshed on the next run.
    async def old_format():
        async with tagenv.provider.async_engine().begin() as conn:
            await tag_storage.merge_stream_state(conn, "p1", lambda st: st.update(
                periodic={k: v["h"] for k, v in st["periodic"].items()}))
    run(old_format())
    run(worker.periodic_profile("p1"))
    assert kinds("health.summary") == 3


def test_the_clear_lift_and_restore_reset_what_was_sent(tagenv) -> None:
    from app.backup.engine import _close_out_tags

    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    enable(tagenv, "p1")
    result = claim(tagenv, tag, "p1", cleared=False)

    async def seed():
        async with tagenv.provider.async_engine().begin() as conn:
            await tag_storage.merge_stream_state(conn, "p1", lambda st: st.update(
                periodic={"health.summary": {"h": "x", "at": 1}}, diag={tag: {"issue": "ok", "at": 1}},
                pruned_through_seq=3))
    run(seed())
    clear_cmd = next(c for c in result["commands"] if c["kind"] == "clear_tag")
    run(tagenv.store.complete_command(hw["companion_id"], clear_cmd["id"], status="succeeded",
                                      result=None, error=None))
    state = run(tagenv.store.get_stream("p1"))["state"]
    assert "periodic" not in state and tag not in (state.get("diag") or {})
    run(seed())
    _close_out_tags(tagenv.engine, [])
    state = run(tagenv.store.get_stream("p1"))["state"]
    assert state == {"pruned_through_seq": 3}


# ── [medium] chat needs-input resolved by any reply ─────────────────────────


def test_a_plan_accept_or_a_plain_reply_resolves_the_chat_question(tagenv) -> None:
    from app.storage.conversation_storage import ConversationStorage

    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p1")
    enable(tagenv, "p1")
    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    worker = TagProjectionWorker(tagenv.store)
    for follow_up in (
        ("user", {"plan_mode": {"stage": "accepted"}}),
        ("user", None),
        ("agent", None),
    ):
        conv = run(cs.create_conversation(profile="p1", title="Deploy"))
        run(cs.add_message(conv["id"], "agent", "Plan", turn=TurnContext(profile="p1"), metadata={
            "plan_mode": {"stage": "awaiting_approval", "plan": {"title": "Ship"}}}))
        run(worker.project_profile("p1"))
        role, meta = follow_up
        run(cs.add_message(conv["id"], role, "ok", metadata=meta,
                           turn=TurnContext(profile="p1", result=(role == "agent"))))
        run(worker.project_profile("p1"))
        key = f"chat:{conv['id']}:input"
        cards = rows(tagenv, "SELECT kind, stage FROM tag_deliveries WHERE replace_key=:k OR resolves=:k "
                             "ORDER BY seq", k=key)
        assert cards == [{"kind": "needs_input", "stage": "superseded"},
                         {"kind": "resolved", "stage": "queued"}], follow_up


# ── [medium] a cancel reaches the companion ─────────────────────────────────


def test_a_cancel_is_sent_on_as_a_resolved_job(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    auth = _content(tagenv, "p1", hw)
    note = run(service.display("p1", tag, {"title": "fetched"}))
    assert note["replace_key"] == f"delivery:{note['id']}"
    page = body_of(connector("events", "GET", auth, query={"after": "0"}))
    connector("accepted", "POST", auth, body={"through_seq": page["next_after"], "delivery_ids": [note["id"]]})
    out = body_of(call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p1",
                       path={"delivery_id": str(note["id"])}))
    assert out["delivery"]["stage"] == "cancelled"
    resolved = out["resolved"]
    assert resolved["kind"] == "resolved" and resolved["resolves"] == f"delivery:{note['id']}"
    jobs = body_of(connector("events", "GET", auth, query={"after": str(page["next_after"])}))["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert {k: job[k] for k in ("kind", "resolves", "replace_key", "tag_id", "stage", "priority")} == {
        "kind": "resolved", "resolves": f"delivery:{note['id']}", "replace_key": None,
        "tag_id": "T1", "stage": "queued", "priority": 90}
    assert job["card"]["kind"] == "resolved" and job["card"]["source"] == {"type": "delivery", "id": str(note["id"])}
    again = call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p1", path={"delivery_id": str(note["id"])})
    assert again.status_code == 409


# ── [low] compare-and-set on stage ──────────────────────────────────────────


def test_receipts_and_cancel_never_reopen_a_concurrent_terminal_state(tagenv, monkeypatch) -> None:
    if tagenv.backend != "postgres":
        pytest.skip("SQLite's single writer makes this interleaving impossible")
    hw = hardware(tagenv, tags=("T1",))
    tag = hw["tags"]["T1"]
    claim(tagenv, tag, "p1")
    auth = _content(tagenv, "p1", hw)
    one = run(service.display("p1", tag, {"title": "one"}))
    two = run(service.display("p1", tag, {"title": "two"}))
    connector("accepted", "POST", auth, body={"delivery_ids": [one["id"], two["id"]]})
    real = tag_storage.cas_stage

    def meddle(stage, delivery_id):
        async def racing(conn, did, expected, values):
            with tagenv.engine.begin() as c:  # another transaction commits first
                c.execute(text("UPDATE tag_deliveries SET stage=:s, outcome=:s WHERE id=:i"),
                          {"s": stage, "i": delivery_id})
            monkeypatch.setattr(tag_storage, "cas_stage", real)
            return await real(conn, did, expected, values)
        monkeypatch.setattr(tag_storage, "cas_stage", racing)

    meddle("cancelled", one["id"])
    out = body_of(connector("receipts", "POST", auth, body={"receipts": [
        {"delivery_id": one["id"], "stage": "transferring"}]}))
    assert out == {"applied": 0, "rejected": [{"delivery_id": one["id"], "reason": "terminal"}]}
    assert scalar(tagenv, "SELECT stage FROM tag_deliveries WHERE id=:i", i=one["id"]) == "cancelled"
    meddle("displayed", two["id"])
    resp = call("/api/tags/deliveries/{delivery_id}/cancel", "POST", "p1", path={"delivery_id": str(two["id"])})
    assert resp.status_code == 409 and body_of(resp)["error"] == "already_terminal"
    assert scalar(tagenv, "SELECT stage FROM tag_deliveries WHERE id=:i", i=two["id"]) == "displayed"


# ── [low] restore close-out: the local counter survives the wipe ────────────


def test_restore_close_out_starts_past_the_local_counter(tagenv) -> None:
    from app.backup.engine import _capture_tag_counter, _close_out_tags

    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_counters SET value=:v WHERE name='delivery_id'"), {"v": 2 ** 32 + 6000})
    local = _capture_tag_counter(tagenv.engine)
    assert local == 2 ** 32 + 6000
    with tagenv.engine.begin() as c:   # what the archive brings back
        c.execute(text("UPDATE tag_counters SET value=5000 WHERE name='delivery_id'"))
    _close_out_tags(tagenv.engine, [], local)
    assert scalar(tagenv, "SELECT value FROM tag_counters") == 2 ** 32 + 6000 + 2 ** 32


# ── [low] a failed enabled-profile read is not cached ───────────────────────


def test_a_failed_enabled_read_is_retried_not_cached(tagenv, monkeypatch) -> None:
    enable(tagenv, "p1")
    journal.invalidate_enabled_cache()
    engine = tagenv.provider.async_engine()

    class Broken:
        def connect(self):
            raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(journal, "_async_engine_of", lambda bind: Broken())
    assert run(journal.enabled_profiles_async(engine)) == frozenset()
    monkeypatch.undo()
    assert run(journal.enabled_profiles_async(engine)) == frozenset({"p1"})

    class Missing:
        def connect(self):
            raise RuntimeError('relation "tag_settings" does not exist')

    journal.invalidate_enabled_cache()
    monkeypatch.setattr(journal, "_async_engine_of", lambda bind: Missing())
    assert run(journal.enabled_profiles_async(engine)) == frozenset()
    monkeypatch.undo()
    assert run(journal.enabled_profiles_async(engine)) == frozenset()  # schema-missing is cached


# ── [low] delete_profile vs an in-flight journalled write ───────────────────


def test_deleting_a_profile_mid_write_does_not_deadlock(tagenv) -> None:
    from app.storage.conversation_storage import ConversationStorage
    from app.storage.models import MessageModel
    from sqlalchemy import insert

    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    run(cs.create_profile("p3"))
    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p3")
    enable(tagenv, "p3")
    conv = run(cs.create_conversation(profile="p3", title="t"))

    async def go():
        engine = tagenv.provider.async_engine()
        inserted = asyncio.Event()

        async def source_write():
            async with engine.begin() as conn:
                await conn.execute(insert(MessageModel.__table__), [{
                    "id": "m-race", "conversation_id": conv["id"], "role": "agent", "content": "x",
                    "created_at": 0, "ordering": 0,
                }])
                inserted.set()
                await asyncio.sleep(0.5)
                await journal.append_async(conn, "p3", [_note("x")])

        writer = asyncio.create_task(source_write())
        await inserted.wait()
        deleted = await cs.delete_profile("p3")
        await writer
        return deleted

    assert run(go()) is True
    assert scalar(tagenv, "SELECT COUNT(*) FROM profiles WHERE name='p3'") == 0
    assert run(tagenv.store.get_device(hw["tags"]["T1"]))["owner_profile"] is None


# ── [low] admin defaults are cached, not read on the loop each batch ────────


def test_admin_defaults_are_read_once_and_invalidated_on_write(tagenv, monkeypatch) -> None:
    import app.config.settings as settings_mod

    reads = []
    store = {}

    def get_dynamic(table, key, default=None, profile=None):
        reads.append(key)
        return store.get(key, default)

    class Cfg:
        def set(self, table, key, value, **kw):
            store[key] = value

    monkeypatch.setattr(settings_mod, "get_dynamic", get_dynamic)
    routing.invalidate_admin_defaults()
    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p1")
    enable(tagenv, "p1")
    worker = TagProjectionWorker(tagenv.store)
    for i in range(3):
        _append(tagenv, "p1", [_note(f"n{i}")])
        run(worker.project_profile("p1"))
    assert reads.count(routing.DEFAULTS_KEY) == 1
    routing.write_admin_defaults({"language": "vi"}, Cfg())
    _append(tagenv, "p1", [_note("after")])
    run(worker.project_profile("p1"))
    assert reads.count(routing.DEFAULTS_KEY) == 2
    assert json.loads(_deliveries(tagenv)[-1]["card"])["lang"] == "vi"


# ── [low] the hardware queue cannot be starved ──────────────────────────────


def test_repeated_identify_is_deduplicated_and_ownership_commands_come_first(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    ids = {run(service.device_command("p1", hw["tags"]["T1"], "identify", requested_by="p1"))["id"]
           for _ in range(60)}
    assert len(ids) == 1
    run(service.device_command("p1", hw["tags"]["T1"], "refresh_tag", requested_by="p1"))
    run(service.claim_tag(hw["tags"]["T2"], owner="p2", requested_by="admin"))
    kinds = [c["kind"] for c in body_of(connector("commands", "GET", hw["auth"], query={"wait": "0"}))["commands"]]
    # T1's own assignment (still queued) and T2's claim come before any identify/refresh.
    assert kinds == ["assign_tag", "assign_tag", "clear_tag", "identify", "refresh_tag"]


# ── [low] autostart failures carry no process output ────────────────────────


def test_autostart_failures_journal_a_fixed_summary(tagenv) -> None:
    from app.storage.autostart_storage import AutostartStorage

    enable(tagenv, "p1")
    store = AutostartStorage(tagenv.provider)
    row = store.insert(profile="p1", command="bot --password hunter2", working_dir="/x/skills/mailbox/scripts",
                       is_pty=False)
    store.set_error(row["id"], "process exited immediately — exit code 1 | stderr: login failed for "
                               "alice@example.com password hunter2 | stdout: ...", profile="p1")
    payload = rows(tagenv, "SELECT payload FROM tag_events")[0]["payload"]
    assert json.loads(payload) == {"automation_kind": "autostart", "name": "mailbox listener",
                                   "error": "exited with code 1"}
    assert "hunter2" not in payload and "alice" not in payload
    entry = sanitize.automation_failed_entry(automation_kind="schedule", name="n", source_id="s",
                                             error="dispatch failed | stderr: secret stuff")
    assert entry.payload["error"] == "dispatch failed"


# ── [low] an OTP on its own line ─────────────────────────────────────────────


def test_a_code_on_its_own_line_is_refused_and_masked(tagenv) -> None:
    assert sanitize.contains_otp("Your verification code:\n482913")
    assert sanitize.contains_otp("PIN\n\n\n            0042")
    assert "482913" not in sanitize.clean_multiline("Your code:\n482913\nthanks", 400)
    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p1")
    resp = call("/api/tags/devices/{device_id}/display", "POST", "p1", path={"device_id": hw["tags"]["T1"]},
                body={"title": "Login", "body": "Your verification code:\n482913"})
    assert resp.status_code == 422 and body_of(resp)["error"] == "otp_refused"


# ── [low] uint32 epochs and one bad inventory item ──────────────────────────


def test_large_epochs_and_a_bad_item_do_not_fail_the_inventory(tagenv) -> None:
    hw = hardware(tagenv, tags=(), bridges=("B1",))
    resp = connector("inventory", "POST", hw["auth"], body={"tags": [
        {"tag_id": "T1", "epoch": 3_000_000_000},
        {"tag_id": "T5", "width": 10 ** 12, "board": "x", "epoch": 2 ** 40, "planes": -1},
    ]})
    assert resp.status_code == 200
    devices = {d["hw_id"]: d for d in body_of(resp)["devices"]}
    assert devices["T1"]["epoch"] == 3_000_000_000
    assert devices["T5"]["epoch"] == 0 and devices["T5"]["width"] is None
    claimed = run(service.claim_tag(devices["T1"]["id"], owner="p1", requested_by="admin"))
    assert claimed["device"]["epoch"] == 3_000_000_001
    note = run(service.clear("p1", devices["T1"]["id"]))
    assert note["epoch"] == 3_000_000_001


# ── [low] senders are masked on tags ────────────────────────────────────────


def test_request_notifications_mask_the_sender() -> None:
    entry = sanitize.notification_entry({
        "kind": "channel_subscribe_request", "conversation_title": "Nguyen Van A",
        "message_preview": "Nguyen Van A wants to subscribe to the zalo notification channel.",
        "channel_type": "zalo", "sender_id": "84901234567", "sender_name": "Nguyen Van A",
    })
    text_out = json.dumps(entry.payload, ensure_ascii=False)
    assert "84901234567" not in text_out and "Van A" not in text_out
    assert entry.payload["title"] == "Zalo access request" and "N… (…4567)" in entry.payload["preview"]
    group = sanitize.notification_entry({
        "kind": "channel_group_request", "conversation_title": "Group request: Family chat",
        "message_preview": "Mom added the agent to Family chat", "channel_type": "telegram",
    })
    assert "Family" not in json.dumps(group.payload) and "Mom" not in json.dumps(group.payload)


# ── [medium] an errored reply shows no text unless excerpts are on ──────────


def test_an_errored_reply_card_hides_the_partial_answer_by_default() -> None:
    event = {"kind": "assistant.result", "created_at": 0, "expires_at": 1e15, "source_type": "conversation",
             "source_id": "c", "replace_key": "chat:c:result",
             "payload": {"conversation_id": "c", "title": "T", "errored": True, "cancelled": False,
                         "excerpt": "my private summary ... then an error"}}
    off = cards_for_event(event, profile="p1", options=routing.effective_options({}, {}))[0]
    on = cards_for_event(event, profile="p1", options=routing.effective_options({"show_excerpts": True}, {}))[0]
    assert off.card["body"] == "The reply failed."
    assert on.card["body"].startswith("my private summary")


# ── API asks from the UI and daemon authors ─────────────────────────────────


def test_overview_pending_count_and_settings_builtin(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1", "T2"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    claim(tagenv, hw["tags"]["T2"], "p1")
    run(service.display("p1", hw["tags"]["T1"], {"title": "a"}))
    run(service.display("p1", hw["tags"]["T1"], {"title": "b"}))
    devices = {d["hw_id"]: d for d in body_of(call("/api/tags", "GET", "p1"))["devices"]}
    assert devices["T1"]["pending_count"] == 2 and devices["T2"]["pending_count"] == 0
    settings = body_of(call("/api/tags/settings", "GET", "p1"))
    assert settings["builtin"] == routing.BUILTIN_DEFAULTS


def test_timezones_are_always_iana_names(monkeypatch) -> None:
    from app.tags import tzmap

    windows = fixed_zone(timedelta(hours=7), "SE Asia Standard Time")
    assert tzmap.iana_name(windows) == "Asia/Bangkok"
    assert tzmap.iana_name(ZoneInfo("Asia/Tokyo")) == "Asia/Tokyo"
    assert tzmap.iana_name(fixed_zone(timedelta(hours=7), "UTC+07:00")) == "Etc/GMT-7"
    assert tzmap.iana_name(fixed_zone(timedelta(hours=-5), "Somewhere")) == "Etc/GMT+5"
    assert tzmap.iana_name(fixed_zone(timedelta(hours=5, minutes=30), "Nowhere")) == "UTC"
    assert routing.resolved_timezone("p1", {"timezone": "+07:00"}) == "Etc/GMT-7"
    assert routing.resolved_timezone("p1", {"timezone": "Europe/Paris"}) == "Europe/Paris"
    import app.config.timezone as tz_mod

    monkeypatch.setattr(tz_mod, "resolve_tzinfo", lambda profile: windows)
    monkeypatch.setattr(tzmap, "_windows_key_name", lambda: None)
    assert routing.resolved_timezone("p1", {"timezone": ""}) == "Asia/Bangkok"


def test_connector_timestamps_have_milliseconds(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    claim(tagenv, hw["tags"]["T1"], "p1")
    run(service.display("p1", hw["tags"]["T1"], {"title": "a"}))
    auth = _content(tagenv, "p1", hw)
    stamp = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
    job = body_of(connector("events", "GET", auth, query={"after": "0"}))["jobs"][0]
    assert stamp.match(job["created_at"]) and stamp.match(job["expires_at"]) and stamp.match(job["card"]["ts"])
    assert stamp.match(body_of(connector("whoami", "GET", auth))["server_time"])


def test_the_preview_revision_header_is_exposed_to_cors() -> None:
    import inspect

    import app.server as server

    source = inspect.getsource(server)
    stack = source[source.index("middleware_stack = ["):]
    assert 'expose_headers=["X-Tag-Revision"]' in stack[:stack.index("]\n")]
