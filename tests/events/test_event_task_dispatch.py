"""One-shot consumption in the run dispatcher, and what tasks do to run rows.

A task that fires twice would inject two continuation turns into a user's chat;
a task consumed by a rejected gate would never fire at all. Both are pinned
here, along with the two run-row fields delivery depends on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.events import run_dispatcher  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.event_run_storage import EventRunStorage  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "event_runs",
    "skill_event_subscriptions",
)


class _Runs(list):
    """Records each run_agent_to_bus call instead of driving a real agent."""

    async def __call__(self, **kwargs):
        self.append(kwargs)


def _setup(tmp_path: Path, monkeypatch, *, agent_fails=False):
    provider = SqliteDatabaseProvider(str(tmp_path / "dispatch.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))

    cs = ConversationStorage(provider)
    cs._initialized = True
    ers = EventRunStorage(provider)
    subs = EventSubscriptionStorage(provider)

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    monkeypatch.setattr(storage_pkg, "get_event_subscription_storage", lambda *a, **k: subs)

    from app.events import runner as event_runner
    monkeypatch.setattr(event_runner, "get_conversation_storage", lambda: cs)
    monkeypatch.setattr(event_runner, "get_cremind_agent", lambda: object())

    runs = _Runs()
    if agent_fails:
        async def _boom(**kwargs):
            raise RuntimeError("agent could not start")
        monkeypatch.setattr("app.agent.stream_runner.run_agent_to_bus", _boom)
    else:
        monkeypatch.setattr("app.agent.stream_runner.run_agent_to_bus", runs)

    forwarded = []

    async def _forward(_storage, origin, run_conv):
        forwarded.append((origin, run_conv))
    monkeypatch.setattr(run_dispatcher, "_maybe_forward_to_channel", _forward)

    delivered = []

    async def _deliver(**kwargs):
        delivered.append(kwargs)
        return "delivered"
    monkeypatch.setattr(
        "app.events.event_task_delivery.on_run_terminal", _deliver, raising=False,
    )

    return cs, ers, subs, runs, forwarded, delivered


async def _task_sub(cs, subs, *, task=True):
    origin = await cs.create_conversation(profile="p1", title="Customer ABC")
    sub = subs.insert(
        conversation_id=origin["id"], profile="p1", skill_name="imap-email",
        event_type="new-mail", action="report the reply", task=task,
    )
    return sub, origin["id"]


def _job(sub, *, task=True, gate=None):
    return {
        "source_kind": "skill_event",
        "subscription_id": sub["id"],
        "profile": "p1",
        "registering_conversation_id": sub["conversation_id"],
        "task": task,
        "label": "imap-email:new-mail",
        "action": sub["action"],
        "query": "report the reply",
        "trigger_event": {"event_type": "new-mail", "action": "x", "content": "mail"},
        "trigger_payload": {},
        "user_metadata": {},
        "gate": gate,
    }


def test_two_triggers_for_one_task_produce_one_run(tmp_path, monkeypatch):
    cs, ers, subs, runs, _, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        sub, _ = await _task_sub(cs, subs)
        await run_dispatcher._execute(_job(sub))
        await run_dispatcher._execute(_job(sub))   # a duplicate/late trigger
        rows, _ = await ers.list(profile="p1")
        return rows, sub

    rows, sub = asyncio.run(_run())
    assert len(rows) == 1
    assert len(runs) == 1
    assert subs.get(sub["id"])["task_status"] == "triggered"


def test_standing_subscription_fires_every_time(tmp_path, monkeypatch):
    cs, ers, subs, runs, _, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        sub, _ = await _task_sub(cs, subs, task=False)
        await run_dispatcher._execute(_job(sub, task=False))
        await run_dispatcher._execute(_job(sub, task=False))
        rows, _ = await ers.list(profile="p1")
        return rows

    assert len(asyncio.run(_run())) == 2
    assert len(runs) == 2


def test_task_run_row_carries_its_origin_and_delivery_flag(tmp_path, monkeypatch):
    cs, ers, subs, _, _, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        sub, origin_id = await _task_sub(cs, subs)
        await run_dispatcher._execute(_job(sub))
        rows, _ = await ers.list(profile="p1")
        return rows[0], origin_id

    row, origin_id = asyncio.run(_run())
    assert row["origin_conversation_id"] == origin_id
    assert row["deliver_to_origin"] is True


def test_gate_rejection_does_not_spend_the_one_shot(tmp_path, monkeypatch):
    """A gate-rejected event never happened; the task must stay armed."""
    cs, ers, subs, runs, _, _ = _setup(tmp_path, monkeypatch)

    class _Reject:
        matched = False
        reason = "unrelated"
        tokens: dict = {}

    async def _classify(**kwargs):
        return _Reject()

    monkeypatch.setattr("app.events.gate.classify_event_match", _classify)

    from app.events import runner as event_runner
    monkeypatch.setattr(
        event_runner, "get_cremind_agent",
        lambda: type("A", (), {"low_performance_llm": lambda self, p: None})(),
    )

    async def _no_usage(**kwargs):
        return None
    monkeypatch.setattr(event_runner, "_record_gate_usage", _no_usage)

    async def _run():
        sub, _ = await _task_sub(cs, subs)
        gate = {"event_type": "new-mail", "action": "a", "file_content": "junk"}
        await run_dispatcher._execute(_job(sub, gate=gate))
        rows, _ = await ers.list(profile="p1")
        return rows, subs.get(sub["id"])

    rows, sub_row = asyncio.run(_run())
    assert rows == []
    assert runs == []
    assert sub_row["task_status"] == "active"     # still waiting for a real one


def test_task_run_output_is_not_mirrored_to_the_channel(tmp_path, monkeypatch):
    """Otherwise a Telegram user gets the raw run AND the continuation reply."""
    cs, _, subs, _, forwarded, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        sub, _ = await _task_sub(cs, subs)
        await run_dispatcher._execute(_job(sub))

    asyncio.run(_run())
    assert forwarded == []


def test_standing_run_still_mirrors_to_the_channel(tmp_path, monkeypatch):
    cs, _, subs, _, forwarded, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        sub, origin_id = await _task_sub(cs, subs, task=False)
        await run_dispatcher._execute(_job(sub, task=False))
        return origin_id

    origin_id = asyncio.run(_run())
    assert len(forwarded) == 1
    assert forwarded[0][0] == origin_id


def test_a_task_whose_run_cannot_start_reports_instead_of_hanging(tmp_path, monkeypatch):
    cs, ers, subs, _, _, delivered = _setup(tmp_path, monkeypatch, agent_fails=True)

    async def _run():
        sub, _ = await _task_sub(cs, subs)
        await run_dispatcher._execute(_job(sub))
        rows, _ = await ers.list(profile="p1")
        return rows[0]

    row = asyncio.run(_run())
    assert row["status"] == "failed"
    assert len(delivered) == 1
    assert delivered[0]["status"] == "failed"
