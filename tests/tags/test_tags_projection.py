"""TagProjectionWorker: journal -> deliveries, and the housekeeping around it.

Pinned: routing (``all`` / a device list / ``none``, never another profile's
tag), supersede by ``(tag, replace_key)``, the needs-input -> resolved pair
(and no ``resolved`` for a card the tag never had), expiry, pruning with the
pruned-through cursor, the ``clear_required`` hold (content waits, the clear
itself does not), delivery ids and seqs allocated without gaps, and the
projection claim that makes a second projector a no-op.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.tags import journal  # noqa: E402
from app.tags.journal import JournalEntry, TurnContext  # noqa: E402
from app.tags.projection import TagProjectionWorker  # noqa: E402
from app.tags.sanitize import turn_entries  # noqa: E402
from tests.tags._helpers import claim, enable, hardware, rows, run, scalar  # noqa: E402


def _append(env, profile, entries):
    async def go():
        async with env.provider.async_engine().begin() as conn:
            return await journal.append_async(conn, profile, entries)
    return run(go())


def _note(title: str, priority: str = "normal") -> JournalEntry:
    return JournalEntry(kind="notification", payload={"kind": "x", "title": title, "preview": "",
                                                      "priority": priority}, source_type="t")


def _deliveries(env, profile="p1"):
    return rows(env, "SELECT d.id, d.seq, d.kind, d.stage, d.replace_key, d.resolves, v.hw_id "
                     "FROM tag_deliveries d JOIN tag_devices v ON v.id = d.tag_device_id "
                     "WHERE d.profile = :p ORDER BY d.id", p=profile)


@pytest.fixture
def owned(tagenv):
    hw = hardware(tagenv, tags=("T1", "T2", "T3"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    claim(tagenv, hw["tags"]["T2"], "p1")
    claim(tagenv, hw["tags"]["T3"], "p2")
    return hw


def test_routes_to_every_owned_tag_and_never_to_another_profiles(tagenv, owned) -> None:
    enable(tagenv, "p1")
    enable(tagenv, "p2")
    _append(tagenv, "p1", [_note("hello")])
    worker = TagProjectionWorker(tagenv.store)
    assert run(worker.project_profile("p1")) == 1
    got = _deliveries(tagenv)
    assert sorted(d["hw_id"] for d in got) == ["T1", "T2"]
    assert [d["seq"] for d in got] == [1, 2]
    assert _deliveries(tagenv, "p2") == []
    # Nothing left: a second pass is a no-op.
    assert run(worker.project_profile("p1")) == 0
    assert scalar(tagenv, "SELECT projected_seq FROM tag_streams WHERE profile='p1'") == 1


def test_route_options_pick_tags_and_drop_foreign_ids(tagenv, owned) -> None:
    t1, t3 = owned["tags"]["T1"], owned["tags"]["T3"]
    enable(tagenv, "p1", routes={"notification": [t1, t3], "task_outcome": "none"})
    _append(tagenv, "p1", [_note("n")] + [
        JournalEntry(kind="run.completed", payload={"run_id": "r", "title": "R", "status": "completed"},
                     source_type="event_run", source_id="r", replace_key="run:r"),
    ])
    run(TagProjectionWorker(tagenv.store).project_profile("p1"))
    got = _deliveries(tagenv)
    assert [(d["kind"], d["hw_id"]) for d in got] == [("notification", "T1")]


def test_a_newer_card_supersedes_the_older_one_on_the_same_key(tagenv, owned) -> None:
    enable(tagenv, "p1", routes={k: "none" for k in ("notification",)})
    turn = TurnContext(profile="p1", conversation_title="Chat")
    first = turn_entries(turn, conversation_id="c1", message_id="m1", role="agent", content="one", metadata=None)
    worker = TagProjectionWorker(tagenv.store)
    _append(tagenv, "p1", first)
    run(worker.project_profile("p1"))
    second = turn_entries(turn, conversation_id="c1", message_id="m2", role="agent", content="two", metadata=None)
    _append(tagenv, "p1", second)
    run(worker.project_profile("p1"))
    got = _deliveries(tagenv)
    by_tag = {}
    for d in got:
        by_tag.setdefault(d["hw_id"], []).append(d["stage"])
    assert by_tag == {"T1": ["superseded", "queued"], "T2": ["superseded", "queued"]}
    # Two in one batch: the first is written already superseded.
    _append(tagenv, "p1", turn_entries(turn, conversation_id="c2", message_id="m3", role="agent",
                                       content="a", metadata=None)
            + turn_entries(turn, conversation_id="c2", message_id="m4", role="agent", content="b", metadata=None))
    run(worker.project_profile("p1"))
    c2 = [d["stage"] for d in _deliveries(tagenv) if d["replace_key"] == "chat:c2:result"]
    assert c2 == ["superseded", "superseded", "queued", "queued"]


def test_needs_input_is_resolved_and_resolution_needs_a_card(tagenv, owned) -> None:
    enable(tagenv, "p1")
    worker = TagProjectionWorker(tagenv.store)
    ask = turn_entries(TurnContext(profile="p1"), conversation_id="c1", message_id="m", role="agent",
                       content="?", metadata={"plan_mode": {"stage": "awaiting_approval", "plan": {"title": "Go"}}})
    _append(tagenv, "p1", ask)
    run(worker.project_profile("p1"))
    resolve = turn_entries(TurnContext(profile="p1", result=False), conversation_id="c1", message_id="m2",
                           role="user", content="", metadata={"plan_mode": {"stage": "cancelled"}})
    _append(tagenv, "p1", resolve)
    run(worker.project_profile("p1"))
    got = _deliveries(tagenv)
    needs = [d for d in got if d["kind"] == "needs_input"]
    resolved = [d for d in got if d["kind"] == "resolved"]
    assert {d["stage"] for d in needs} == {"superseded"}
    assert sorted(d["hw_id"] for d in resolved) == ["T1", "T2"]
    assert {d["resolves"] for d in resolved} == {"chat:c1:input"}
    # Resolving again (or something never shown) routes nowhere.
    _append(tagenv, "p1", resolve + turn_entries(
        TurnContext(profile="p1", result=False), conversation_id="never", message_id="m3", role="user",
        content="", metadata={"plan_mode": {"stage": "cancelled"}}))
    before = len(_deliveries(tagenv))
    run(worker.project_profile("p1"))
    assert len(_deliveries(tagenv)) == before


def test_ask_and_answer_in_one_batch(tagenv, owned) -> None:
    enable(tagenv, "p1")
    ask = JournalEntry(kind="run.needs_input", payload={"run_id": "r1", "title": "R", "question": "Ok?"},
                       source_type="event_run", source_id="r1", replace_key="run:r1:input")
    answer = JournalEntry(kind="run.resumed", payload={"run_id": "r1", "title": "R"},
                          source_type="event_run", source_id="r1", replace_key="run:r1:input")
    _append(tagenv, "p1", [ask, answer])
    run(TagProjectionWorker(tagenv.store).project_profile("p1"))
    stages = [(d["kind"], d["stage"]) for d in _deliveries(tagenv)]
    assert sorted(stages) == [("needs_input", "superseded"), ("needs_input", "superseded"),
                              ("resolved", "queued"), ("resolved", "queued")]


def test_clear_required_holds_content_until_the_clear_is_confirmed(tagenv) -> None:
    hw = hardware(tagenv, tags=("T1",))
    result = claim(tagenv, hw["tags"]["T1"], "p1", cleared=False)
    enable(tagenv, "p1")
    worker = TagProjectionWorker(tagenv.store)
    _append(tagenv, "p1", [_note("held")])
    run(worker.project_profile("p1"))
    assert _deliveries(tagenv) == []
    clear_cmd = next(c for c in result["commands"] if c["kind"] == "clear_tag")
    # A result for an older epoch does not lift the hold.
    stale = run(tagenv.store.create_command(companion_id=hw["companion_id"], kind="clear_tag",
                                            args={"tag_id": "T1", "epoch": 0}, requested_by="t", ttl_s=60))
    run(tagenv.store.complete_command(hw["companion_id"], stale["id"], status="succeeded",
                                      result=None, error=None))
    assert scalar(tagenv, "SELECT clear_required FROM tag_devices WHERE hw_id='T1'") == 1
    run(tagenv.store.complete_command(hw["companion_id"], clear_cmd["id"], status="succeeded",
                                      result=None, error=None))
    assert scalar(tagenv, "SELECT clear_required FROM tag_devices WHERE hw_id='T1'") == 0
    _append(tagenv, "p1", [_note("now")])
    run(worker.project_profile("p1"))
    assert [d["kind"] for d in _deliveries(tagenv)] == ["notification"]


def test_expiry_and_pruning(tagenv, owned) -> None:
    enable(tagenv, "p1")
    worker = TagProjectionWorker(tagenv.store)
    _append(tagenv, "p1", [_note("a"), _note("b")])
    run(worker.project_profile("p1"))
    ids = [d["id"] for d in _deliveries(tagenv)]
    past = time.time() * 1000 - 1000
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_deliveries SET expires_at=:p WHERE id=:i"), {"p": past, "i": ids[0]})
    assert run(worker.expire(time.time() * 1000)) == 1
    assert _deliveries(tagenv)[0]["stage"] == "expired"
    old = time.time() * 1000 - 31 * 86_400_000
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_deliveries SET finished_at=:o WHERE id=:i"), {"o": old, "i": ids[0]})
        c.execute(text("UPDATE tag_events SET created_at=:o"), {"o": old})
    run(worker.prune(time.time() * 1000))
    left = _deliveries(tagenv)
    assert [d["id"] for d in left] == ids[1:]
    stream = run(tagenv.store.get_stream("p1"))
    assert stream["state"]["pruned_through_seq"] == 1
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 0


def test_a_second_projector_cannot_project_the_same_events(tagenv, owned) -> None:
    enable(tagenv, "p1")
    _append(tagenv, "p1", [_note("once")])
    a, b = TagProjectionWorker(tagenv.store), TagProjectionWorker(tagenv.store)

    async def both():
        import asyncio

        return await asyncio.gather(a.project_profile("p1"), b.project_profile("p1"))

    run(both())
    assert len(_deliveries(tagenv)) == 2  # one per owned tag, not four


def test_tick_projects_only_enabled_profiles_with_a_backlog(tagenv, owned) -> None:
    enable(tagenv, "p1")
    _append(tagenv, "p1", [_note("x")])
    _append(tagenv, "p2", [_note("not enabled")])
    worker = TagProjectionWorker(tagenv.store)
    worker._last_maintenance = worker._last_periodic = time.monotonic()
    assert run(tagenv.store.profiles_with_backlog()) == ["p1", "p2"]
    run(worker.tick())
    assert len(_deliveries(tagenv)) == 2 and _deliveries(tagenv, "p2") == []
    assert run(tagenv.store.profiles_with_backlog()) == ["p2"]


def test_the_worker_wakes_on_a_journal_commit(tagenv, owned) -> None:
    import asyncio

    enable(tagenv, "p1")

    async def go():
        worker = TagProjectionWorker(tagenv.store)
        worker._last_maintenance = worker._last_periodic = time.monotonic()
        worker.start(asyncio.get_running_loop())
        try:
            await asyncio.sleep(0.05)
            await journal.append_standalone("p1", [_note("wake")])
            for _ in range(40):
                await asyncio.sleep(0.05)
                if scalar(tagenv, "SELECT COUNT(*) FROM tag_deliveries"):
                    return True
            return False
        finally:
            worker.stop()

    assert run(go()) is True


def test_delivery_ids_come_from_the_counter(tagenv, owned) -> None:
    enable(tagenv, "p1")
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_counters SET value=41 WHERE name='delivery_id'"))
    _append(tagenv, "p1", [_note("x")])
    run(TagProjectionWorker(tagenv.store).project_profile("p1"))
    assert [d["id"] for d in _deliveries(tagenv)] == [42, 43]
    assert scalar(tagenv, "SELECT value FROM tag_counters") == 43


def test_diagnostics_are_journalled_on_change_only(tagenv, owned) -> None:
    enable(tagenv, "p1")
    journal.configure_standalone(tagenv.provider)
    worker = TagProjectionWorker(tagenv.store)
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_devices SET battery_mv=2100 WHERE hw_id='T1'"))
    now = time.time() * 1000
    run(worker.diagnostics("p1", now))
    run(worker.diagnostics("p1", now))
    kinds = [r["kind"] for r in rows(tagenv, "SELECT kind FROM tag_events WHERE profile='p1'")]
    assert kinds == ["tag.diagnostics"]
    run(worker.project_profile("p1"))
    diag = [d for d in _deliveries(tagenv) if d["kind"] == "tag_diagnostics"]
    assert [d["hw_id"] for d in diag] == ["T1"]


def test_run_progress_is_sampled_from_the_todo_list(tagenv, owned, monkeypatch) -> None:
    from app.agent import plan_state
    import app.storage as storage_pkg
    from app.storage.event_run_storage import EventRunStorage

    enable(tagenv, "p1")
    journal.configure_standalone(tagenv.provider)
    ers = EventRunStorage(tagenv.provider)
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    created = run(ers.create(profile="p1", source_kind="schedule", subscription_id="s", conversation_id=None,
                             label="Backup photos", action="a", run_id="run-xyz"))
    todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]
    monkeypatch.setattr(plan_state, "get_todos", lambda rid: todos if rid == "run-xyz" else None)
    worker = TagProjectionWorker(tagenv.store)
    run(worker.sample_progress(["p1"], time.time() * 1000))
    run(worker.sample_progress(["p1"], time.time() * 1000))  # unchanged: nothing new
    events = rows(tagenv, "SELECT kind, payload FROM tag_events WHERE kind='run.progress'")
    assert len(events) == 1
    import json

    assert json.loads(events[0]["payload"])["done"] == 1 and json.loads(events[0]["payload"])["total"] == 2
    run(worker.project_profile("p1"))
    progress = [d for d in _deliveries(tagenv) if d["kind"] == "progress"]
    # run.started and run.progress share the run's key: the newer one replaced it.
    assert sorted(d["stage"] for d in progress) == ["queued", "queued", "superseded", "superseded"]
    assert {d["replace_key"] for d in progress} == {f"run:{created['run']['id']}"}


def test_periodic_content_is_journalled_when_its_hash_changes(tagenv, owned, monkeypatch) -> None:
    enable(tagenv, "p1", routes={"calendar": "none", "automation": "none", "usage": "none",
                                 "indexing_problem": "none"})
    worker = TagProjectionWorker(tagenv.store)
    payload = {"title": "1 channel problem(s)", "body": "telegram: stopped", "count": 1}

    async def health(profile, options):
        return dict(payload)

    monkeypatch.setattr(worker, "_health", health)
    run(worker.periodic_profile("p1"))
    run(worker.periodic_profile("p1"))
    payload["count"] = 2
    run(worker.periodic_profile("p1"))
    kinds = [r["kind"] for r in rows(tagenv, "SELECT kind FROM tag_events WHERE profile='p1'")]
    assert kinds == ["health.summary", "health.summary"]
