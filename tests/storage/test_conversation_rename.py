"""Regression: renaming a conversation id repoints EVERY FK child table.

``rename_conversation_id`` rewrites the ``conversations.id`` primary key under
``PRAGMA defer_foreign_keys=ON`` (FKs re-checked at COMMIT), so it must repoint
every child row that references the old id or the commit fails with
``FOREIGN KEY constraint failed``. It previously repointed only 3 of the 6
tables that FK-reference ``conversations.id`` — ``usage_records`` (one row per
LLM call), ``channel_senders``, and ``schedule_event_subscriptions`` were
missed, so any active conversation blew up on rename. The fix derives the child
tables from the schema, so this test seeds a row in all six and asserts the
rename succeeds and moves every row to the new id.

Harness mirrors ``tests/storage/test_usage_latest_context.py`` (real on-disk
SQLite, tables from ORM metadata). Because that harness sets ``_initialized``
directly, it skips ``initialize()`` — so we call ``apply_pragmas`` ourselves to
arm ``foreign_keys=ON`` on the async engine, exactly as production does. Without
it the deferred FK check wouldn't run and this test couldn't catch the bug.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from sqlalchemy import func, select  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.models import (  # noqa: E402
    ChannelModel,
    ChannelSenderModel,
    ConversationModel,
    FileWatcherSubscriptionModel,
    MessageModel,
    ProfileModel,
    ScheduleEventSubscriptionModel,
    SkillEventSubscriptionModel,
    UsageRecordModel,
)

_TABLES = (
    "profiles",
    "channels",
    "conversations",
    "messages",
    "usage_records",
    "channel_senders",
    "skill_event_subscriptions",
    "file_watcher_subscriptions",
    "schedule_event_subscriptions",
)

# Every table that FK-references conversations.id — the rename must move all of
# these. Kept explicit (not derived) so the test is an independent check on the
# schema-driven loop in rename_conversation_id.
_CHILD_MODELS = (
    MessageModel,
    UsageRecordModel,
    ChannelSenderModel,
    SkillEventSubscriptionModel,
    FileWatcherSubscriptionModel,
    ScheduleEventSubscriptionModel,
)

_OLD = "c1-old"
_NEW = "c2-new"


def _make_storage(tmp_path: Path) -> ConversationStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "conv.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    store = ConversationStorage(provider)
    store._initialized = True
    return store


async def _seed(store: ConversationStorage) -> None:
    """Seed a profile + channel + conversation and one row in each FK child."""
    now = time.time() * 1000
    async with store.async_session_maker.begin() as s:
        # models.py defines no ORM relationship(), so the unit-of-work can't
        # infer parent->child insert ordering from the FK columns. Under
        # foreign_keys=ON that ordering matters, so flush each parent before the
        # rows that reference it.
        s.add(ProfileModel(id="p", name="admin", created_at=now, updated_at=now))
        await s.flush()
        s.add(ChannelModel(
            id="ch", profile="admin", channel_type="main",
            created_at=now, updated_at=now,
        ))
        await s.flush()
        s.add(ConversationModel(
            id=_OLD, profile="admin", channel_id="ch", title="t",
            created_at=now, updated_at=now,
        ))
        await s.flush()
        s.add(MessageModel(
            id="m1", conversation_id=_OLD, role="user", created_at=now,
        ))
        s.add(UsageRecordModel(
            id="u1", conversation_id=_OLD, profile="admin", created_at=now,
        ))
        s.add(ChannelSenderModel(
            id="cs1", channel_id="ch", sender_id="s1", conversation_id=_OLD,
            created_at=now, updated_at=now,
        ))
        s.add(SkillEventSubscriptionModel(
            id="se1", conversation_id=_OLD, profile="admin", skill_name="sk",
            event_type="e", action="a", created_at=now,
        ))
        s.add(FileWatcherSubscriptionModel(
            id="fw1", conversation_id=_OLD, profile="admin", name="n",
            root_path="/tmp", event_types="created", action="a", created_at=now,
        ))
        s.add(ScheduleEventSubscriptionModel(
            id="sch1", conversation_id=_OLD, profile="admin",
            dtstart="2026-01-01T00:00:00", created_at=now, updated_at=now,
        ))


def test_rename_repoints_every_fk_child_table(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)

    async def run() -> None:
        # Arm foreign_keys=ON on the async engine (initialize() normally does
        # this; the test harness skips initialize()). Without it the deferred
        # FK check at COMMIT is a no-op and the bug is invisible.
        async with store.engine.begin() as conn:
            await store.provider.apply_pragmas(conn)

        await _seed(store)

        # The pre-fix code raised sqlalchemy.exc.IntegrityError here.
        renamed = await store.rename_conversation_id(_OLD, _NEW)
        assert renamed is not None
        assert renamed["id"] == _NEW

        async with store.async_session_maker() as s:
            # Parent row moved.
            assert (await s.execute(
                select(func.count()).select_from(ConversationModel)
                .where(ConversationModel.id == _OLD)
            )).scalar() == 0
            # Every child row followed the rename — none stranded at the old id.
            for model in _CHILD_MODELS:
                stale = (await s.execute(
                    select(func.count()).select_from(model)
                    .where(model.conversation_id == _OLD)
                )).scalar()
                moved = (await s.execute(
                    select(func.count()).select_from(model)
                    .where(model.conversation_id == _NEW)
                )).scalar()
                assert stale == 0, f"{model.__tablename__} still points at old id"
                assert moved == 1, f"{model.__tablename__} not repointed to new id"

    asyncio.run(run())
