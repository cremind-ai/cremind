"""The Tags journal: an ordered, per-profile log of allowlisted events.

Every entry is appended **inside the transaction that changes the state it
describes** (an assistant message, an event-run status, a channel failure …),
so a rolled-back change leaves no entry and a committed one always has its
entry. Callers pass their own ``AsyncSession`` / ``AsyncConnection``
(:func:`append_async`) or sync ``Connection`` (:func:`append_sync`) and append
LAST, just before commit:

1. ``UPDATE tag_streams SET next_seq = next_seq + k WHERE profile = :p
   RETURNING next_seq`` — inserting the head row first when it is missing
   (``ON CONFLICT DO NOTHING``);
2. insert ``k`` rows with consecutive seqs.

On PostgreSQL the head-row lock is held until commit, so a second writer for
the same profile waits and takes the next seqs: commit order equals seq order
and a rolled-back writer leaves no gap. SQLite has a single writer. A cursor
reader therefore never sees seq N+1 before N.

Only profiles with Tags enabled are journalled. Whether a profile is enabled
comes from a 5-second cache of ``tag_settings.enabled`` per database
(:func:`invalidate_enabled_cache` on every settings save), refreshed on its
own short connection so a missing table or a failed read can never poison
the caller's transaction. For a profile without Tags the hooks cost that
cache check and nothing else.

Standalone events (notifications, dispatch failures, the projection worker's
periodic content) have no source transaction: :func:`append_standalone`
commits them on their own, and that commit is the acceptance boundary.

Payloads are built by :mod:`app.tags.sanitize` — allowlisted fields only,
never row diffs, reasoning, tool or terminal output.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.storage.models import TagEventModel, TagSettingsModel, TagStreamModel
from app.utils.logger import logger

DURABLE = "durable"
CHECKPOINT = "checkpoint"

_HOUR = 3600.0
_DAY = 24 * _HOUR

# The allowlist: kind -> (durability, default card lifetime in seconds).
KINDS: dict[str, tuple[str, float]] = {
    "assistant.result": (DURABLE, _DAY),
    "chat.needs_input": (DURABLE, 7 * _DAY),
    "chat.needs_input_resolved": (DURABLE, 7 * _DAY),
    "run.started": (CHECKPOINT, _HOUR),
    "run.progress": (CHECKPOINT, _HOUR),
    "run.needs_input": (DURABLE, 7 * _DAY),
    "run.resumed": (DURABLE, 7 * _DAY),
    "run.completed": (DURABLE, _DAY),
    "run.failed": (DURABLE, _DAY),
    "channel.failed": (DURABLE, _DAY),
    "channel.unlinked": (DURABLE, _DAY),
    "channel.recovered": (DURABLE, _DAY),
    "automation.failed": (DURABLE, _DAY),
    "subscription.changed": (DURABLE, _DAY),
    "notification": (DURABLE, _DAY),
    "tag.diagnostics": (CHECKPOINT, _DAY),
    "calendar.upcoming": (CHECKPOINT, _DAY),
    "automation.upcoming": (CHECKPOINT, _DAY),
    "usage.summary": (CHECKPOINT, _DAY),
    "indexing.problem": (CHECKPOINT, _DAY),
    "health.summary": (CHECKPOINT, _DAY),
}

_STREAMS = TagStreamModel.__table__
_EVENTS = TagEventModel.__table__
_SETTINGS = TagSettingsModel.__table__


@dataclass(frozen=True)
class JournalEntry:
    """One entry to append. Build it through :mod:`app.tags.sanitize`."""

    kind: str
    payload: dict[str, Any]
    source_type: str
    source_id: str | None = None
    replace_key: str | None = None
    ttl_s: float | None = None


@dataclass(frozen=True)
class TurnContext:
    """What ``ConversationStorage.add_message`` needs to journal a turn without
    a query of its own: the caller already holds the conversation row."""

    profile: str
    conversation_kind: str = "chat"
    conversation_title: str = ""
    errored: bool = False
    cancelled: bool = False
    # False for a message that is not a turn's answer (the plan-cancel marker):
    # only its needs-input resolution is journalled.
    result: bool = True


# ── enabled-profile cache ───────────────────────────────────────────────────

_CACHE_TTL_S = 5.0
_cache: dict[tuple, tuple[float, frozenset[str]]] = {}
_cache_lock = threading.Lock()


def _url_of(bind: Any):
    if isinstance(bind, (Engine, AsyncEngine)):
        return bind.url
    if isinstance(bind, AsyncSession):
        return bind.bind.url
    if isinstance(bind, (Connection, AsyncConnection)):
        return bind.engine.url
    raise TypeError(f"not a database handle: {type(bind).__name__}")


def _db_key(bind: Any) -> tuple:
    """One key per DATABASE, shared by its async and sync engines."""
    url = _url_of(bind)
    return (url.get_backend_name(), url.host or "", url.port or 0, url.database or "")


def _fresh(key: tuple) -> frozenset[str] | None:
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    return None


def _store(key: tuple, value: frozenset[str]) -> frozenset[str]:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def invalidate_enabled_cache() -> None:
    """Forget every cached enabled set (called on each settings save)."""
    with _cache_lock:
        _cache.clear()


def _async_engine_of(bind: Any) -> AsyncEngine:
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncSession):
        return bind.bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    raise TypeError(f"not an async database handle: {type(bind).__name__}")


def _sync_engine_of(bind: Any) -> Engine:
    if isinstance(bind, Engine):
        return bind
    if isinstance(bind, Connection):
        return bind.engine
    raise TypeError(f"not a sync database handle: {type(bind).__name__}")


_ENABLED_QUERY = select(_SETTINGS.c.profile).where(_SETTINGS.c.enabled.is_(True))


def _read_failed(key: tuple, exc: BaseException) -> frozenset[str]:
    """A failed read. A database without the Tags tables (an old schema, a
    test built from a few tables) really has nobody enabled, so that is
    cached like a result; anything else (a dropped connection, a timeout) is
    NOT cached — the next hook reads again instead of silently skipping every
    profile's journal for the whole cache window."""
    text = str(exc).lower()
    if "no such table" in text or "does not exist" in text or "undefinedtable" in text:
        logger.debug("[tags] no tag_settings table; journal disabled", exc_info=True)
        return _store(key, frozenset())
    logger.warning(f"[tags] enabled-profile read failed ({type(exc).__name__}: {exc}); "
                   "skipping this journal entry, retrying on the next one")
    return frozenset()


async def enabled_profiles_async(bind: Any) -> frozenset[str]:
    """Profiles with Tags enabled on this database (cached for 5 s)."""
    key = _db_key(bind)
    hit = _fresh(key)
    if hit is not None:
        return hit
    try:
        async with _async_engine_of(bind).connect() as conn:
            rows = (await conn.execute(_ENABLED_QUERY)).scalars().all()
    except Exception as exc:  # noqa: BLE001
        return _read_failed(key, exc)
    return _store(key, frozenset(str(r) for r in rows))


def enabled_profiles_sync(bind: Any) -> frozenset[str]:
    key = _db_key(bind)
    hit = _fresh(key)
    if hit is not None:
        return hit
    try:
        with _sync_engine_of(bind).connect() as conn:
            rows = conn.execute(_ENABLED_QUERY).scalars().all()
    except Exception as exc:  # noqa: BLE001
        return _read_failed(key, exc)
    return _store(key, frozenset(str(r) for r in rows))


async def is_enabled_async(bind: Any, profile: str | None) -> bool:
    if not profile:
        return False
    return profile in await enabled_profiles_async(bind)


def is_enabled_sync(bind: Any, profile: str | None) -> bool:
    if not profile:
        return False
    return profile in enabled_profiles_sync(bind)


# ── appends ─────────────────────────────────────────────────────────────────


def _dialect_name(bind: Any) -> str:
    dialect = getattr(bind, "dialect", None)
    if dialect is None and isinstance(bind, AsyncSession):
        dialect = bind.bind.dialect
    return dialect.name


def insert_ignore(dialect_name: str, table, values: dict[str, Any], keys: list[str]):
    """``INSERT … ON CONFLICT (keys) DO NOTHING`` for SQLite and PostgreSQL."""
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert
    return _insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)


def new_stream_values(profile: str, now_ms: float) -> dict[str, Any]:
    return {
        "profile": profile,
        "stream_id": str(uuid.uuid4()),
        "next_seq": 0,
        "projected_seq": 0,
        "next_delivery_seq": 0,
        "state": {},
        "updated_at": now_ms,
    }


def _bump(profile: str, k: int, now_ms: float):
    return (
        update(_STREAMS)
        .where(_STREAMS.c.profile == profile)
        .values(next_seq=_STREAMS.c.next_seq + k, updated_at=now_ms)
        .returning(_STREAMS.c.next_seq)
    )


def _allowed(entries: Iterable[JournalEntry]) -> list[JournalEntry]:
    out: list[JournalEntry] = []
    for entry in entries:
        if entry.kind not in KINDS:
            logger.warning(f"[tags] dropping journal entry of unknown kind {entry.kind!r}")
            continue
        out.append(entry)
    return out


def _rows(profile: str, entries: Sequence[JournalEntry], first_seq: int, now_ms: float) -> list[dict]:
    rows = []
    for i, entry in enumerate(entries):
        durability, ttl = KINDS[entry.kind]
        rows.append({
            "id": str(uuid.uuid4()),
            "profile": profile,
            "seq": first_seq + i,
            "kind": entry.kind,
            "durability": durability,
            "replace_key": entry.replace_key,
            "source_type": entry.source_type,
            "source_id": entry.source_id,
            "payload": dict(entry.payload or {}),
            "created_at": now_ms,
            "expires_at": now_ms + float(entry.ttl_s or ttl) * 1000.0,
        })
    return rows


async def append_async(bind: AsyncSession | AsyncConnection, profile: str,
                       entries: Sequence[JournalEntry]) -> list[int]:
    """Append inside the caller's async transaction. Returns the new seqs.

    Does NOT check whether the profile has Tags enabled — callers decide
    (usually through :func:`is_enabled_async`, before their transaction).
    """
    entries = _allowed(entries)
    if not entries:
        return []
    now_ms = time.time() * 1000
    k = len(entries)
    head = (await bind.execute(_bump(profile, k, now_ms))).scalar_one_or_none()
    if head is None:
        await bind.execute(insert_ignore(
            _dialect_name(bind), _STREAMS, new_stream_values(profile, now_ms), ["profile"],
        ))
        head = (await bind.execute(_bump(profile, k, now_ms))).scalar_one()
    first = int(head) - k + 1
    await bind.execute(insert(_EVENTS), _rows(profile, entries, first, now_ms))
    return list(range(first, int(head) + 1))


def append_sync(conn: Connection, profile: str, entries: Sequence[JournalEntry]) -> list[int]:
    """:func:`append_async` for a sync ``Connection`` inside ``engine.begin()``."""
    entries = _allowed(entries)
    if not entries:
        return []
    now_ms = time.time() * 1000
    k = len(entries)
    head = conn.execute(_bump(profile, k, now_ms)).scalar_one_or_none()
    if head is None:
        conn.execute(insert_ignore(
            conn.dialect.name, _STREAMS, new_stream_values(profile, now_ms), ["profile"],
        ))
        head = conn.execute(_bump(profile, k, now_ms)).scalar_one()
    first = int(head) - k + 1
    conn.execute(insert(_EVENTS), _rows(profile, entries, first, now_ms))
    return list(range(first, int(head) + 1))


# ── call-site intents ───────────────────────────────────────────────────────
#
# ``ConversationStorage.update_channel`` / ``update_sender`` take ``**fields``
# that are column names, and the adapters that call them are routinely given
# test doubles. Rather than a keyword those doubles would store as a column,
# the few call sites whose write must be journalled wrap it:
#
#     with journal.intent(profile, [sanitize.channel_entry(...)]):
#         await storage.update_channel(channel_id, enabled=False, state=state)
#
# and the real store takes the intent inside its transaction. Task-local.

_intent: ContextVar[tuple[str, tuple[JournalEntry, ...]] | None] = ContextVar(
    "tags_journal_intent", default=None,
)


@contextmanager
def intent(profile: str | None, entries: Sequence[JournalEntry]):
    token = _intent.set((profile, tuple(entries)) if profile and entries else None)
    try:
        yield
    finally:
        _intent.reset(token)


def take_intent() -> tuple[str, tuple[JournalEntry, ...]] | None:
    """Consume the pending intent (the first store write in the block wins)."""
    value = _intent.get()
    if value is not None:
        _intent.set(None)
    return value


# ── projection wake-up ──────────────────────────────────────────────────────

_wake_cb: Callable[[], None] | None = None


def set_wake_callback(cb: Callable[[], None] | None) -> None:
    global _wake_cb
    _wake_cb = cb


def wake() -> None:
    """Nudge the projection worker after a journal commit (thread-safe)."""
    cb = _wake_cb
    if cb is None:
        return
    try:
        cb()
    except Exception:  # noqa: BLE001
        logger.debug("[tags] projection wake failed", exc_info=True)


# ── standalone appends ──────────────────────────────────────────────────────

_standalone_provider: Any = None
_pending: set[asyncio.Task] = set()


def configure_standalone(provider: Any) -> None:
    """Enable standalone appends against ``provider`` (``None`` disables them).

    Set by the projection worker at boot, so nothing is journalled standalone
    before storage is up."""
    global _standalone_provider
    _standalone_provider = provider


async def append_standalone(profile: str, entries: Sequence[JournalEntry]) -> list[int]:
    """Commit ``entries`` in a transaction of their own, when Tags is enabled."""
    provider = _standalone_provider
    if provider is None or not entries or not profile:
        return []
    engine = provider.async_engine()
    if not await is_enabled_async(engine, profile):
        return []
    async with engine.begin() as conn:
        seqs = await append_async(conn, profile, entries)
    if seqs:
        wake()
    return seqs


def _append_standalone_sync(provider: Any, profile: str, entries: Sequence[JournalEntry]) -> None:
    engine = provider.sync_engine()
    if not is_enabled_sync(engine, profile):
        return
    with engine.begin() as conn:
        seqs = append_sync(conn, profile, entries)
    if seqs:
        wake()


def submit_standalone(profile: str, entries: Sequence[JournalEntry]) -> None:
    """Fire-and-forget :func:`append_standalone` from synchronous code.

    On the event loop it schedules a task; from a worker thread it writes
    synchronously (blocking that thread only). A profile the cache already
    knows is disabled costs nothing. Never raises.
    """
    provider = _standalone_provider
    if provider is None or not entries or not profile:
        return
    try:
        cached = _fresh(_db_key(provider.async_engine()))
        if cached is not None and profile not in cached:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            _append_standalone_sync(provider, profile, entries)
            return

        async def _run() -> None:
            try:
                await append_standalone(profile, entries)
            except Exception:  # noqa: BLE001
                logger.warning(f"[tags] standalone journal append failed for {profile!r}", exc_info=True)

        task = loop.create_task(_run(), name="tags-journal-standalone")
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:  # noqa: BLE001
        logger.warning(f"[tags] standalone journal append failed for {profile!r}", exc_info=True)


async def drain_standalone() -> None:
    """Wait for scheduled standalone appends (tests, shutdown)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
