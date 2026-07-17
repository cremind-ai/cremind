"""Event-run cascade delete keeps usage; conversation delete tears down bound runs.

Covers the user's core integrity requirements:
- deleting an event rule deletes its runs + hidden conversations, but Usage &
  Cost totals are unchanged (usage rows survive);
- the run conversations really are removed.
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
from app.storage.event_run_storage import EventRunStorage  # noqa: E402
from app.storage.usage_storage import UsageStorage  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "usage_records", "event_runs",
)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "lc.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid','p1',0,0)"))

    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)
    us = UsageStorage(provider)

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    return provider, cs, ers, us


async def _make_run(cs, ers, us, sub_id, status="completed"):
    conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
    created = await ers.create(profile="p1", source_kind="schedule", subscription_id=sub_id,
                               conversation_id=conv["id"], label="L", action="a")
    rid = created["run"]["id"]
    await ers.update_status(rid, status=status, mark_finished=(status != "pending"))
    await us.add_usage_records(conv["id"], "p1",
        [{"source_kind": "reasoning", "input_tokens": 10, "output_tokens": 5}],
        message_id=None, event_run_id=rid)
    return rid, conv["id"]


def test_rule_delete_cascades_runs_keeps_usage(tmp_path: Path, monkeypatch) -> None:
    provider, cs, ers, us = _setup(tmp_path, monkeypatch)
    from app.events import run_lifecycle

    async def run():
        _, conv1 = await _make_run(cs, ers, us, "delsub")
        _, conv2 = await _make_run(cs, ers, us, "delsub")
        before = (await us.totals(profile="p1"))["total_tokens"]
        assert before == 30

        n = await run_lifecycle.delete_runs_for_subscription("schedule", "delsub", "p1")
        assert n == 2

        _, total = await ers.list(subscription_id="delsub")
        assert total == 0, "runs not deleted"
        assert await cs.get_conversation(conv1) is None, "run conversation survived"
        assert await cs.get_conversation(conv2) is None, "run conversation survived"

        after = (await us.totals(profile="p1"))["total_tokens"]
        assert after == before, "usage lost on rule delete"

    asyncio.run(run())


def test_subscription_summaries(tmp_path: Path, monkeypatch) -> None:
    provider, cs, ers, us = _setup(tmp_path, monkeypatch)

    async def run():
        await _make_run(cs, ers, us, "subA", status="completed")
        # Sleep so the pending run's created_at is strictly later — it is the
        # "most recent" run whose status last_status must report.
        await asyncio.sleep(0.01)
        await _make_run(cs, ers, us, "subA", status="pending")
        summaries = await ers.subscription_summaries("p1")
        s = summaries["schedule:subA"]
        assert s["run_count"] == 2
        assert s["pending_count"] == 1
        assert s["active_count"] == 0
        assert s["last_run_at"] is not None
        assert s["last_status"] == "pending"

    asyncio.run(run())
