"""The Tags journal: ordering, same-transaction hooks, isolation, OTP safety.

- Commit order is seq order per profile, a rolled-back appender leaves no gap,
  and a cursor reader polling throughout never sees one (SQLite here; the same
  scenario runs on PostgreSQL in ``test_tags_migration_pg.py``).
- Every hook writes its entry in the SAME transaction as the state: when the
  source write rolls back, so does the entry.
- A profile without Tags costs nothing beyond the cached enabled check — the
  statement count of a hooked write is the same as an unhooked one.
- A one-time code is never journalled: the ``channel_otp`` push, a preview
  carrying a code, an ``otp`` field in ``extra``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("a2a")

from sqlalchemy import event, insert, select, text  # noqa: E402

from app.storage.models import TagCompanionModel, TagEventModel  # noqa: E402
from app.tags import journal, sanitize  # noqa: E402
from app.tags.journal import JournalEntry, TurnContext  # noqa: E402
from tests.tags._helpers import enable, rows, run, scalar  # noqa: E402

EVENTS = TagEventModel.__table__
COMPANIONS = TagCompanionModel.__table__


def _entry(name: str) -> JournalEntry:
    return JournalEntry(kind="notification", payload={"kind": "x", "title": name},
                        source_type="test", source_id=name)


async def interleaved_appends(provider, profile: str):
    """Four writers on one profile with overlapping transactions; C rolls
    back. Returns ``(commits, observed)``: ``(writer, seq)`` in commit order
    and every snapshot of seqs a concurrent reader saw."""
    engine = provider.async_engine()
    commits: list[tuple[str, int]] = []
    observed: list[list[int]] = []
    stop = asyncio.Event()

    async def reader() -> None:
        while not stop.is_set():
            async with engine.connect() as conn:
                seqs = (await conn.execute(
                    select(EVENTS.c.seq).where(EVENTS.c.profile == profile).order_by(EVENTS.c.seq)
                )).scalars().all()
            observed.append([int(s) for s in seqs])
            await asyncio.sleep(0.005)

    async def writer(name: str, start: float, hold: float, fail: bool = False) -> None:
        await asyncio.sleep(start)
        try:
            async with engine.begin() as conn:
                # The source write this entry describes (a distinct row each).
                await conn.execute(insert(COMPANIONS), [{
                    "id": str(uuid.uuid4()), "name": name, "created_by": "",
                    "created_at": 0, "updated_at": 0,
                }])
                seqs = await journal.append_async(conn, profile, [_entry(name)])
                await asyncio.sleep(hold)
                if fail:
                    raise RuntimeError("roll back")
            commits.append((name, seqs[0]))
        except RuntimeError:
            pass

    task = asyncio.create_task(reader())
    await asyncio.gather(
        writer("A", 0.0, 0.3),
        writer("B", 0.05, 0.0),
        writer("C", 0.1, 0.1, fail=True),
        writer("D", 0.15, 0.05),
    )
    await asyncio.sleep(0.02)
    stop.set()
    await task
    async with engine.connect() as conn:
        final = (await conn.execute(
            select(EVENTS.c.seq).where(EVENTS.c.profile == profile).order_by(EVENTS.c.seq)
        )).scalars().all()
    observed.append([int(s) for s in final])
    return commits, observed


def test_commit_order_is_seq_order_and_a_reader_sees_no_gap(tagenv) -> None:
    commits, observed = run(interleaved_appends(tagenv.provider, "p1"))
    assert [n for n, _ in commits][0] == "A"
    assert sorted(n for n, _ in commits) == ["A", "B", "D"]
    seqs = [s for _, s in commits]
    assert seqs == sorted(seqs) == [1, 2, 3]  # C rolled back and left no hole
    assert len(observed) > 3
    for snapshot in observed:
        assert snapshot == list(range(1, len(snapshot) + 1))
    assert scalar(tagenv, "SELECT next_seq FROM tag_streams WHERE profile='p1'") == 3


def test_append_is_per_profile(tagenv) -> None:
    async def go():
        async with tagenv.provider.async_engine().begin() as conn:
            a = await journal.append_async(conn, "p1", [_entry("a"), _entry("b")])
            b = await journal.append_async(conn, "p2", [_entry("c")])
            c = await journal.append_async(conn, "p1", [_entry("d")])
        return a, b, c

    assert run(go()) == ([1, 2], [1], [3])
    with tagenv.engine.begin() as conn:
        assert journal.append_sync(conn, "p2", [_entry("e")]) == [2]


def test_unknown_kinds_are_dropped(tagenv) -> None:
    async def go():
        async with tagenv.provider.async_engine().begin() as conn:
            return await journal.append_async(conn, "p1", [JournalEntry("row.diff", {}, "x")])

    assert run(go()) == []
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 0


def test_enabled_cache_follows_settings_saves(tagenv) -> None:
    eng = tagenv.provider.async_engine()
    assert run(journal.is_enabled_async(eng, "p1")) is False
    enable(tagenv, "p1")
    assert run(journal.is_enabled_async(eng, "p1")) is True
    assert journal.is_enabled_sync(tagenv.engine, "p1") is True
    assert run(journal.is_enabled_async(eng, "p2")) is False
    run(tagenv.store.save_settings("p1", enabled=False))
    assert run(journal.is_enabled_async(eng, "p1")) is False


# ── hooks: same transaction ──────────────────────────────────────────────────


def _conversation_storage(tagenv):
    from app.storage.conversation_storage import ConversationStorage

    cs = ConversationStorage(tagenv.provider)
    run(cs.initialize())
    return cs


def test_assistant_result_is_journalled_with_the_message(tagenv) -> None:
    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    conv = run(cs.create_conversation(profile="p1", title="Trip plan"))
    run(cs.add_message(conv["id"], "agent", "Here is the plan. Bearer abcdefghijklmnop123",
                       turn=TurnContext(profile="p1", conversation_title="Trip plan")))
    events = rows(tagenv, "SELECT kind, payload, replace_key, source_id FROM tag_events WHERE profile='p1'")
    assert [e["kind"] for e in events] == ["assistant.result"]
    import json

    payload = json.loads(events[0]["payload"])
    assert payload["title"] == "Trip plan" and payload["conversation_id"] == conv["id"]
    assert "abcdefghijklmnop123" not in payload["excerpt"]
    assert events[0]["replace_key"] == f"chat:{conv['id']}:result"


def test_hidden_conversations_do_not_produce_assistant_results(tagenv) -> None:
    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    for kind in ("event_run", "group_chat", "channel_group"):
        conv = run(cs.create_conversation(profile="p1", title="x", kind="event_run"))
        run(cs.add_message(conv["id"], "agent", "done",
                           turn=TurnContext(profile="p1", conversation_kind=kind)))
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 0


def test_plan_mode_needs_input_and_its_resolution(tagenv) -> None:
    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    conv = run(cs.create_conversation(profile="p1", title="Deploy"))
    turn = TurnContext(profile="p1", conversation_title="Deploy")
    run(cs.add_message(conv["id"], "agent", "Questions", turn=turn, metadata={
        "plan_mode": {"stage": "awaiting_answers", "questions": [{"question": "Which region?"}]},
    }))
    run(cs.add_message(conv["id"], "user", "Cancel this plan.", metadata={"plan_mode": {"stage": "cancelled"}},
                       turn=TurnContext(profile="p1", result=False)))
    kinds = [r["kind"] for r in rows(tagenv, "SELECT kind FROM tag_events ORDER BY seq")]
    assert kinds == ["assistant.result", "chat.needs_input", "chat.needs_input_resolved"]


def test_a_rolled_back_message_leaves_no_journal_entry(tagenv, monkeypatch) -> None:
    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    conv = run(cs.create_conversation(profile="p1", title="t"))
    real = journal.append_async

    async def append_then_fail(*a, **k):
        await real(*a, **k)
        raise RuntimeError("the source transaction fails after the append")

    monkeypatch.setattr(journal, "append_async", append_then_fail)
    with pytest.raises(RuntimeError):
        run(cs.add_message(conv["id"], "agent", "x", turn=TurnContext(profile="p1")))
    assert scalar(tagenv, "SELECT COUNT(*) FROM messages WHERE conversation_id=:c", c=conv["id"]) == 0
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 0
    assert scalar(tagenv, "SELECT next_seq FROM tag_streams WHERE profile='p1'") == 0


def test_event_run_transitions_and_rollback(tagenv, monkeypatch) -> None:
    from app.storage.event_run_storage import EventRunStorage

    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    ers = EventRunStorage(tagenv.provider)
    conv = run(cs.create_conversation(profile="p1", title="r", kind="event_run"))
    created = run(ers.create(profile="p1", source_kind="schedule", subscription_id="s1",
                             conversation_id=conv["id"], label="Nightly report", action="a"))
    rid = created["run"]["id"]
    run(ers.update_status(rid, status="running", profile="p1"))  # no change: nothing journalled
    run(ers.update_status(rid, status="pending", pending_question="Send it now?"))
    run(ers.update_status(rid, status="running", clear_pending=True, profile="p1"))
    run(ers.update_status(rid, status="completed", mark_finished=True, profile="p1"))
    kinds = [r["kind"] for r in rows(tagenv, "SELECT kind FROM tag_events ORDER BY seq")]
    assert kinds == ["run.started", "run.needs_input", "run.resumed", "run.started", "run.completed"]

    real = journal.append_async

    async def append_then_fail(*a, **k):
        await real(*a, **k)
        raise RuntimeError("boom")

    monkeypatch.setattr(journal, "append_async", append_then_fail)
    with pytest.raises(RuntimeError):
        run(ers.update_status(rid, status="failed", error="x", profile="p1"))
    assert scalar(tagenv, "SELECT status FROM event_runs WHERE id=:i", i=rid) == "completed"
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 5


def test_restart_recovery_journals_each_failed_run(tagenv) -> None:
    from app.storage.event_run_storage import EventRunStorage

    enable(tagenv, "p1")
    ers = EventRunStorage(tagenv.provider)
    for profile in ("p1", "p1", "p2"):
        run(ers.create(profile=profile, source_kind="schedule", subscription_id="s",
                       conversation_id=None, label="L", action="a"))
    assert run(ers.recover_after_restart()) == 3
    kinds = rows(tagenv, "SELECT profile, kind FROM tag_events ORDER BY seq")
    assert [(r["profile"], r["kind"]) for r in kinds] == [
        ("p1", "run.started"), ("p1", "run.started"), ("p1", "run.failed"), ("p1", "run.failed"),
    ]


def test_autostart_failure_is_journalled_on_the_same_connection(tagenv) -> None:
    from app.storage.autostart_storage import AutostartStorage

    enable(tagenv, "p1")
    store = AutostartStorage(tagenv.provider)
    row = store.insert(profile="p1", command="uv run listener.py", working_dir="w", is_pty=False)
    other = store.insert(profile="p2", command="uv run other.py", working_dir="w", is_pty=False)
    store.set_error(row["id"], "binary not found", profile="p1")
    store.set_error(other["id"], "binary not found")  # p2 has no Tags
    store.clear_error(row["id"])
    events = rows(tagenv, "SELECT profile, kind FROM tag_events")
    assert events == [{"profile": "p1", "kind": "automation.failed"}]


def test_channel_and_sender_intents(tagenv) -> None:
    enable(tagenv, "p1")
    cs = _conversation_storage(tagenv)
    ch = run(cs.create_channel("p1", "telegram"))
    with journal.intent("p1", [sanitize.channel_entry("channel.failed", channel=ch, error="token revoked")]):
        run(cs.update_channel(ch["id"], enabled=False))
    run(cs.update_channel(ch["id"], enabled=True))  # no intent: nothing
    sender = run(cs.get_or_create_sender(ch["id"], "5551234567", "Jane"))
    # Issuing an OTP is not an access change.
    run(cs.update_sender(sender["id"], pending_otp="123456"))
    with journal.intent("p1", sanitize.access_change_entries(
        channel_id=ch["id"], channel_type="telegram", sender=sender, subscribed=True,
    )):
        run(cs.update_sender(sender["id"], authenticated=True))
    events = rows(tagenv, "SELECT kind, payload FROM tag_events ORDER BY seq")
    assert [e["kind"] for e in events] == ["channel.failed", "subscription.changed"]
    assert "5551234567" not in events[1]["payload"] and "123456" not in events[1]["payload"]


def test_a_disabled_profile_costs_no_extra_statement(tagenv) -> None:
    enable(tagenv, "p1")  # another profile has Tags; p2 does not
    cs = _conversation_storage(tagenv)
    conv = run(cs.create_conversation(profile="p2", title="t"))
    engine = cs.engine.sync_engine
    run(journal.enabled_profiles_async(cs.engine))  # warm the cache
    counted: list[str] = []

    def _count(conn, cursor, statement, *a):
        counted.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        run(cs.add_message(conv["id"], "agent", "plain"))
        plain = len(counted)
        counted.clear()
        run(cs.add_message(conv["id"], "agent", "hooked", turn=TurnContext(profile="p2")))
        hooked = len(counted)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert hooked == plain
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events") == 0


# ── OTP safety ──────────────────────────────────────────────────────────────


def test_otp_detection_and_redaction() -> None:
    assert sanitize.contains_otp("OTP 123456 for telegram")
    assert sanitize.contains_otp("Your verification code is 4821")
    assert sanitize.contains_otp("482913 is your login code")
    assert sanitize.contains_otp("PIN: 0042")
    assert not sanitize.contains_otp("Build 1234 finished")
    assert not sanitize.contains_otp("Meeting at 10:30 in room B")
    red = sanitize.redact("code 123456, api_key=sk-abcdefghijklmnopqrstu Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijk.xyzxyzxyzxyz")
    assert "123456" not in red and "sk-abcdefghijklmnopqrstu" not in red and "eyJhbGci" not in red
    assert "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9" not in sanitize.redact("token aB3dE5fG7hJ9kL1mN3pQ5rS7tU9 here")
    assert sanitize.redact("see https://example.com/some-document-name-2024") .endswith("2024")


def test_otp_notifications_are_never_journalled(tagenv) -> None:
    from app.events.notifications_buffer import EventNotificationsBuffer

    enable(tagenv, "p1")
    journal.configure_standalone(tagenv.provider)
    buf = EventNotificationsBuffer()

    async def go():
        buf.push(profile="p1", conversation_id="", conversation_title="Jane",
                 message_preview="OTP 123456 for telegram", kind="channel_otp", priority="high",
                 extra={"otp": "123456", "channel_id": "c"})
        buf.push(profile="p1", conversation_id="", conversation_title="Heads-up",
                 message_preview="Your code is 482913", kind="skill_register_required")
        buf.push(profile="p1", conversation_id="", conversation_title="Sub",
                 message_preview="Alert", kind="channel_subscribe_request", extra={"otp": "999999"})
        buf.push(profile="p1", conversation_id="", conversation_title="Access request: telegram",
                 message_preview="Jane wants to chat", kind="channel_subscribe_request")
        buf.push(profile="p1", conversation_id="c1", conversation_title="Chat",
                 message_preview="done", kind="completed")
        buf.push(profile="p2", conversation_id="", conversation_title="p2 only",
                 message_preview="hello", kind="channel_subscribe_request")
        await journal.drain_standalone()

    run(go())
    events = rows(tagenv, "SELECT profile, kind, payload FROM tag_events")
    assert [(e["profile"], e["kind"]) for e in events] == [("p1", "notification")]
    assert "Jane wants to chat" in events[0]["payload"]
    all_payloads = " ".join(e["payload"] for e in events)
    assert "123456" not in all_payloads and "482913" not in all_payloads


def test_submit_standalone_from_a_thread_writes_synchronously(tagenv) -> None:
    import threading

    enable(tagenv, "p1")
    journal.configure_standalone(tagenv.provider)
    t = threading.Thread(target=journal.submit_standalone, args=("p1", [_entry("x")]))
    t.start()
    t.join()
    assert scalar(tagenv, "SELECT COUNT(*) FROM tag_events WHERE profile='p1'") == 1
