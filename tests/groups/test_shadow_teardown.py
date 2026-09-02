"""Deleting a seat has to take everything homed on it with it.

A seat is not scratch space. A member can register automations from it, so it
owns rules, their run history and their hidden run conversations exactly like an
ordinary chat — and those hidden conversations are invisible to every listing
and every bulk delete, so the per-rule cascade is the ONLY thing that ever
reclaims them.

The ordering is the whole trick: the cascade finds those rules BY conversation
id, and the FK cascade behind ``delete_conversation`` deletes them first. Run it
the wrong way round and the teardown is a silent no-op that leaks run rows,
hidden conversations and live watchdog observers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.storage as storage_module  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.groups.shadow import delete_shadow_conversation, shadow_context_id  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)


def _env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "seats.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','dog',0,0)"
        ))

    conversation_storage = ConversationStorage(provider)
    conversation_storage._initialized = True
    storage = GroupChatStorage(provider)
    monkeypatch.setattr(
        storage_module, "get_group_chat_storage", lambda *a, **k: storage,
    )
    return SimpleNamespace(
        storage=storage, conversation_storage=conversation_storage,
    )


async def _seat(env):
    group = await env.storage.create_group(name="Ops", members=["dog"])
    conv = await env.conversation_storage.create_conversation(
        profile="dog",
        context_id=shadow_context_id(group["id"], "dog"),
        title="Group: Ops",
        kind="group_chat",
    )
    await env.storage.set_shadow_conversation(group["id"], "dog", conv["id"])
    return group, conv


def test_a_seat_cascades_its_dependents_before_it_is_deleted(tmp_path, monkeypatch):
    """Ordering, pinned from inside the cascade itself."""
    env = _env(tmp_path, monkeypatch)
    seen: list[tuple[str, bool]] = []

    async def _fake_cleanup(conversation_storage, conversation_id):
        # The row must still be there: this is what makes the cascade able to
        # find the rules bound to it.
        row = await conversation_storage.get_conversation(conversation_id)
        seen.append((conversation_id, row is not None))

    monkeypatch.setattr(
        "app.reset._conversations.cleanup_conversation_dependents", _fake_cleanup,
    )

    async def run():
        group, conv = await _seat(env)
        await delete_shadow_conversation(
            env.conversation_storage, group["id"], "dog",
        )
        return group, conv

    group, conv = asyncio.run(run())

    assert seen == [(conv["id"], True)]
    assert asyncio.run(env.conversation_storage.get_conversation(conv["id"])) is None
    member = asyncio.run(env.storage.get_member(group["id"], "dog"))
    assert (member or {}).get("shadow_conversation_id") is None


def test_a_failing_cascade_still_removes_the_seat(tmp_path, monkeypatch):
    """Teardown is best-effort per seat: one bad rule must not strand a room."""
    env = _env(tmp_path, monkeypatch)

    async def _boom(conversation_storage, conversation_id):
        raise RuntimeError("a watcher refused to disarm")

    monkeypatch.setattr(
        "app.reset._conversations.cleanup_conversation_dependents", _boom,
    )

    async def run():
        group, conv = await _seat(env)
        await delete_shadow_conversation(
            env.conversation_storage, group["id"], "dog",
        )
        return conv

    conv = asyncio.run(run())
    assert asyncio.run(env.conversation_storage.get_conversation(conv["id"])) is None
