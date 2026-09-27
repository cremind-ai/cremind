"""Journal and delivery ordering under real concurrency, on PostgreSQL.

SQLite has one writer, so only PostgreSQL READ COMMITTED can commit
transactions out of the order they took their numbers in. These pin the
guarantees a companion's cursor relies on:

1. many transactions append to ONE profile's journal, each holding its
   transaction open for a different time; a reader that keeps reading
   ``seq > cursor`` and advancing never sees a gap that is filled later, commit
   order is seq order, and the committed seqs are dense (rolled-back writers
   leave no hole). A control run that allocates the seq OUTSIDE the source
   transaction shows the checker does catch the failure;
2. two profiles append in parallel without waiting for each other, while two
   writers on one profile do serialise;
3. the projection worker's batch and concurrent REST ``display`` calls (on
   this profile and another) allocate delivery ids and per-profile delivery
   seqs without duplicates or holes, and the connector's ``events`` pages never
   skip a seq that commits later.

The same-transaction rollback tests, and the rest of ``tests/tags``, run on
PostgreSQL through the ``tagenv`` fixture's ``postgres`` parameter.

Skipped unless ``CREMIND_TEST_POSTGRES_URL`` names a THROWAWAY database (every
test WIPES its ``public`` schema) and ``psycopg``/``asyncpg`` are importable.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid

import pytest

pytest.importorskip("a2a")

from tests.tags._helpers import PG_URL  # noqa: E402

if not PG_URL:
    pytest.skip("CREMIND_TEST_POSTGRES_URL is not set (needs a throwaway database)",
                allow_module_level=True)

from sqlalchemy import insert, select, update  # noqa: E402

from app.storage.models import TagCompanionModel, TagEventModel, TagStreamModel  # noqa: E402
from app.tags import journal  # noqa: E402
from app.tags.journal import JournalEntry  # noqa: E402
from app.tags.projection import TagProjectionWorker  # noqa: E402
from tests.tags._helpers import claim, enable, hardware, rows, run, scalar  # noqa: E402

pytestmark = pytest.mark.parametrize("tagenv", ["postgres"], indirect=True)

EVENTS = TagEventModel.__table__
STREAMS = TagStreamModel.__table__
COMPANIONS = TagCompanionModel.__table__


def _entry(name: str) -> JournalEntry:
    return JournalEntry(kind="notification", payload={"kind": "x", "title": name, "preview": "",
                                                      "priority": "normal"}, source_type="test")


async def _source_write(conn, name: str) -> None:
    """The state change a journal entry describes (a distinct row each)."""
    await conn.execute(insert(COMPANIONS), [{
        "id": str(uuid.uuid4()), "name": name, "created_by": "", "created_at": 0, "updated_at": 0,
    }])


class CursorReader:
    """Reads ``seq > cursor`` in a loop, like a companion, and advances."""

    def __init__(self, engine, profile: str):
        self.engine = engine
        self.profile = profile
        self.cursor = 0
        self.seen: list[int] = []
        self.batches: list[tuple[int, list[int]]] = []
        self.reads = 0

    async def read_once(self) -> list[int]:
        async with self.engine.connect() as conn:
            seqs = [int(s) for s in (await conn.execute(
                select(EVENTS.c.seq).where(EVENTS.c.profile == self.profile, EVENTS.c.seq > self.cursor)
                .order_by(EVENTS.c.seq)
            )).scalars().all()]
        self.reads += 1
        if seqs:
            self.batches.append((self.cursor, seqs))
            self.seen.extend(seqs)
            self.cursor = seqs[-1]
        return seqs

    async def run(self, stop: asyncio.Event) -> None:
        while True:
            finished = stop.is_set()
            seqs = await self.read_once()
            if finished and not seqs:
                return
            await asyncio.sleep(0.002)

    def later_filled(self, final: list[int]) -> list[int]:
        """Seqs that committed below a cursor the reader had already passed."""
        return sorted(set(final) - set(self.seen))

    def gapped_batches(self) -> list[tuple[int, list[int]]]:
        return [(c, s) for c, s in self.batches if s != list(range(c + 1, c + 1 + len(s)))]


async def _final_seqs(engine, profile: str) -> list[int]:
    async with engine.connect() as conn:
        return [int(s) for s in (await conn.execute(
            select(EVENTS.c.seq).where(EVENTS.c.profile == profile).order_by(EVENTS.c.seq)
        )).scalars().all()]


# ── 1. commit order = seq order, no gap a reader could pass ──────────────────


def test_concurrent_appends_on_one_profile_never_hide_a_seq(tagenv) -> None:
    engine = tagenv.provider.async_engine()
    rng = random.Random(20260930)
    n = 16
    failing = {3, 8, 13}
    # A later starter usually holds its transaction for less time, which is
    # exactly what would let it commit first without the head-row lock.
    plan = [(i * 0.015, rng.uniform(0.0, 0.25), i in failing) for i in range(n)]
    commits: list[tuple[int, float]] = []

    async def writer(i: int, start: float, hold: float, fail: bool) -> None:
        await asyncio.sleep(start)
        try:
            async with engine.begin() as conn:
                await _source_write(conn, f"w{i}")
                seqs = await journal.append_async(conn, "p1", [_entry(f"w{i}")])
                await asyncio.sleep(hold)
                if fail:
                    raise RuntimeError("the source write fails")
            commits.append((seqs[0], time.monotonic()))
        except RuntimeError:
            pass

    async def go():
        reader = CursorReader(engine, "p1")
        stop = asyncio.Event()
        task = asyncio.create_task(reader.run(stop))
        await asyncio.gather(*(writer(i, *p) for i, p in enumerate(plan)))
        stop.set()
        await task
        return reader, await _final_seqs(engine, "p1")

    reader, final = run(go())
    committed = n - len(failing)
    assert final == list(range(1, committed + 1))
    by_commit_time = [seq for seq, _ in sorted(commits, key=lambda c: c[1])]
    assert by_commit_time == sorted(by_commit_time)
    assert reader.later_filled(final) == []
    assert reader.gapped_batches() == []
    assert reader.reads > 20 and len(reader.batches) > 1
    assert scalar(tagenv, "SELECT next_seq FROM tag_streams WHERE profile='p1'") == committed


def test_the_checker_catches_a_seq_taken_outside_the_source_transaction(tagenv) -> None:
    """Control: take the seq in a transaction of its own (the mistake the
    head-row lock exists to prevent) and PostgreSQL commits 2 before 1."""
    engine = tagenv.provider.async_engine()
    with tagenv.engine.begin() as c:
        c.execute(insert(STREAMS), [journal.new_stream_values("p1", 0)])

    async def naive(name: str, start: float, hold: float) -> None:
        await asyncio.sleep(start)
        async with engine.begin() as conn:
            seq = (await conn.execute(
                update(STREAMS).where(STREAMS.c.profile == "p1")
                .values(next_seq=STREAMS.c.next_seq + 1).returning(STREAMS.c.next_seq)
            )).scalar_one()
        async with engine.begin() as conn:
            await conn.execute(insert(EVENTS), [{
                "id": str(uuid.uuid4()), "profile": "p1", "seq": seq, "kind": "notification",
                "durability": "durable", "source_type": "test", "payload": {}, "created_at": 0,
                "expires_at": 0,
            }])
            await asyncio.sleep(hold)

    async def go():
        reader = CursorReader(engine, "p1")
        stop = asyncio.Event()
        task = asyncio.create_task(reader.run(stop))
        await asyncio.gather(naive("slow", 0.0, 0.4), naive("fast", 0.05, 0.0))
        stop.set()
        await task
        return reader, await _final_seqs(engine, "p1")

    reader, final = run(go())
    assert final == [1, 2]
    assert reader.later_filled(final) == [1]


# ── 2. profiles do not wait for each other ───────────────────────────────────


def test_two_profiles_append_in_parallel_one_profile_serialises(tagenv) -> None:
    engine = tagenv.provider.async_engine()

    async def writer(profile: str, start: float, hold: float, t0: float) -> float:
        await asyncio.sleep(start)
        async with engine.begin() as conn:
            await _source_write(conn, f"{profile}-{start}")
            await journal.append_async(conn, profile, [_entry(profile)])
            await asyncio.sleep(hold)
        return time.monotonic() - t0

    async def go():
        t0 = time.monotonic()
        return await asyncio.gather(
            writer("p1", 0.0, 1.0, t0),   # holds p1's head row for a second
            writer("p2", 0.1, 0.0, t0),   # another profile: must not wait
            writer("p1", 0.1, 0.0, t0),   # the same profile: must wait
        )

    p1_slow, p2_fast, p1_second = run(go())
    assert p2_fast < 0.6, f"p2 waited {p2_fast:.2f}s for p1's transaction"
    assert p1_second >= p1_slow - 0.05 and p1_second > 0.9
    assert scalar(tagenv, "SELECT next_seq FROM tag_streams WHERE profile='p1'") == 2
    assert scalar(tagenv, "SELECT next_seq FROM tag_streams WHERE profile='p2'") == 1


# ── 3. delivery allocation: projection batch + REST display ─────────────────


def test_projection_and_display_allocate_without_holes(tagenv) -> None:
    from app.tags import service

    hw = hardware(tagenv, tags=("T1", "T2", "T3"))
    claim(tagenv, hw["tags"]["T1"], "p1")
    claim(tagenv, hw["tags"]["T2"], "p1")
    claim(tagenv, hw["tags"]["T3"], "p2")
    enable(tagenv, "p1")
    enable(tagenv, "p2")
    journal.configure_standalone(tagenv.provider)
    engine = tagenv.provider.async_engine()
    store = tagenv.store
    rng = random.Random(7)

    async def seed():
        async with engine.begin() as conn:
            await journal.append_async(conn, "p1", [_entry(f"e{i}") for i in range(40)])

    run(seed())
    worker = TagProjectionWorker(store)
    displays_p1 = 12
    displays_p2 = 6
    late_events = 10

    async def project_until_idle() -> None:
        for _ in range(50):
            await worker.project_profile("p1")
            await asyncio.sleep(0.01)

    async def display(profile: str, tag: str, i: int) -> None:
        await asyncio.sleep(rng.uniform(0.0, 0.3))
        await service.display(profile, hw["tags"][tag], {"title": f"note {i}"})

    async def late_journal() -> None:
        for i in range(late_events):
            await asyncio.sleep(rng.uniform(0.0, 0.05))
            await journal.append_standalone("p1", [_entry(f"late{i}")])

    pages: list[tuple[int, list[int], int]] = []

    async def connector_reader(stop: asyncio.Event) -> list[int]:
        cursor, seen = 0, []
        while True:
            finished = stop.is_set()
            page = await store.jobs_after("p1", hw["companion_id"], cursor, 25)
            seqs = [j["seq"] for j in page["jobs"]]
            pages.append((cursor, seqs, page["next_after"]))
            seen.extend(seqs)
            cursor = page["next_after"]
            if finished and not seqs:
                return seen
            await asyncio.sleep(0.003)

    async def go():
        stop = asyncio.Event()
        reader = asyncio.create_task(connector_reader(stop))
        await asyncio.gather(
            project_until_idle(),
            late_journal(),
            *(display("p1", "T1" if i % 2 else "T2", i) for i in range(displays_p1)),
            *(display("p2", "T3", i) for i in range(displays_p2)),
        )
        await project_until_idle()
        stop.set()
        return await reader

    seen = run(go())
    p1 = rows(tagenv, "SELECT id, seq FROM tag_deliveries WHERE profile='p1' ORDER BY seq")
    p2 = rows(tagenv, "SELECT id, seq FROM tag_deliveries WHERE profile='p2' ORDER BY seq")
    expected_p1 = (40 + late_events) * 2 + displays_p1
    assert [r["seq"] for r in p1] == list(range(1, expected_p1 + 1))
    assert [r["seq"] for r in p2] == list(range(1, displays_p2 + 1))
    ids = [r["id"] for r in p1 + p2]
    assert len(set(ids)) == len(ids)
    assert scalar(tagenv, "SELECT value FROM tag_counters WHERE name='delivery_id'") == max(ids)
    # Within one profile the id order follows the seq order (the stream row
    # is locked before the counter row).
    assert [r["id"] for r in p1] == sorted(r["id"] for r in p1)
    # The connector never skipped a seq that committed later.
    assert seen == list(range(1, expected_p1 + 1))
    for cursor, seqs, _ in pages:
        assert seqs == list(range(cursor + 1, cursor + 1 + len(seqs)))
    assert len(pages) > 5
