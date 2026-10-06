"""Tags redraw once by themselves after an upgrade changes how screens look or which font pack draws them."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.tags.runtime.compose import api as compose_api
from app.tags.runtime.compose.api import ActiveCard, ScreenSettings, TagPanel
from app.tags.runtime.connector.models import ProfileSettings, SyncResult, TagInfo
from app.tags.runtime.daemon import open_database
from app.tags.runtime.daemon.screens import SETUP_OVERRIDE, content_key
from app.tags.runtime.daemon.store import QueueStore
from app.tags.runtime.protocol.ids import GATEWAY_ADDR, Status
from app.tags.runtime.sim.harness import run_scenario
from app.tags.runtime.store import BridgeRecord, TagRecord

pytestmark = pytest.mark.timeout(150)

OWNED, EXPIRED, BLOCKED, IDENTIFY, SETUP, CLEARING, RELEASED, ELSEWHERE = range(0x1A2B3C01, 0x1A2B3C09)
MARKER = f"{compose_api.COMPOSER_VERSION}:{'00' * 8}"


def drawn_with(s: QueueStore) -> str | None:
    with s.db.reading() as conn:
        row = conn.execute("SELECT value FROM daemon_state WHERE key = 'screens_drawn_with'").fetchone()
    return row[0] if row else None


def tag_info(tag_id: int, *, clear_required: bool = False) -> TagInfo:
    return TagInfo(tag_id, f"Tag {tag_id:08X}", 1, "br-" + "0" * 32, 400, 300, 1, 0, 0, 0, clear_required)


def sync_result(tags: list[TagInfo]) -> SyncResult:
    return SyncResult(profile="alice", companion_id="c1", stream_id="s1", cursor_valid=True, oldest_seq=1,
                      head_seq=0, outstanding=(), tags=tuple(tags), settings=ProfileSettings())


def test_a_fresh_database_only_records_what_screens_are_drawn_with(tmp_path: Path) -> None:
    s = QueueStore(open_database(tmp_path / "c.sqlite3"))
    assert drawn_with(s) is None
    assert s.redraw_if_changed(MARKER) == 0
    assert drawn_with(s) == MARKER
    assert s.redraw_if_changed(MARKER) == 0


def test_a_redraw_marks_each_tag_showing_our_screens_once(tmp_path: Path) -> None:
    now = 1000.0
    db = open_database(tmp_path / "c.sqlite3")
    db.upsert_bridge(BridgeRecord(f"{2:032x}", addr=2, configured=True))
    for tag_id in (OWNED, EXPIRED, BLOCKED, IDENTIFY, SETUP, CLEARING, RELEASED):  # ELSEWHERE: not enrolled here
        db.insert_tag(TagRecord(tag_id, 16, 1, 400, 300, 1, 1, f"file:tag:{tag_id:08X}", epoch=1, bridge_addr=2))
    s = QueueStore(db, clock=lambda: now)
    owned = [tag_info(t) for t in (OWNED, EXPIRED, BLOCKED, IDENTIFY, SETUP, ELSEWHERE)]
    s.apply_sync("cred", sync_result([*owned, tag_info(CLEARING, clear_required=True), tag_info(RELEASED)]))
    s.apply_sync("cred", sync_result([*owned, tag_info(CLEARING, clear_required=True)]))  # RELEASED was released
    s.block_tag(BLOCKED, "auth_failed", "stopped", status_code=int(Status.AUTH_FAILED), fail_jobs=False)
    s.set_override(IDENTIFY, "identify", now + 60)
    s.set_override(SETUP, SETUP_OVERRIDE + "CTAG:1:setup-code", now + 86400)
    s.set_override(EXPIRED, "identify", now - 1)  # its hold ended: no override any more
    with s.db.transaction() as conn:  # every screen is up to date
        conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0")
    views = {v.tag_id: v for v in s.list_views()}
    assert views[RELEASED].credential_id is None and views[CLEARING].clear_required

    assert s.redraw_if_changed(MARKER) == 2
    views = {v.tag_id: v for v in s.list_views()}
    assert {t for t, v in views.items() if v.dirty} == {OWNED, EXPIRED}
    assert not any(v.force for v in views.values())  # dirty, not forced: an unchanged layout is not sent again
    assert {OWNED, EXPIRED} <= set(s.tags_needing_work()[0])
    assert drawn_with(s) == MARKER

    gens = {t: v.dirty_gen for t, v in views.items()}
    assert s.redraw_if_changed(MARKER) == 0  # the same design and pack again: nothing
    assert {v.tag_id: v.dirty_gen for v in s.list_views()} == gens
    assert s.redraw_if_changed(f"{compose_api.COMPOSER_VERSION}:{'11' * 8}") == 2  # another font pack
    assert {v.tag_id for v in s.list_views() if v.dirty_gen != gens[v.tag_id]} == {OWNED, EXPIRED}


def test_a_redraw_waits_for_bridges_still_on_another_font_pack(tmp_path: Path) -> None:
    db = open_database(tmp_path / "c.sqlite3")
    own, stale, current, unknown = range(0x2B3C4D01, 0x2B3C4D05)
    # The gateway's own radio records the pack the host drew with before: the host renders, so it redraws.
    for addr, pack in ((GATEWAY_ADDR, "aa" * 8), (2, "aa" * 8), (3, "00" * 8), (4, None)):
        db.upsert_bridge(BridgeRecord(f"{addr:032x}", addr=addr, configured=True, fontpack_id=pack))
    for tag_id, addr in ((own, GATEWAY_ADDR), (stale, 2), (current, 3), (unknown, 4)):
        db.insert_tag(TagRecord(tag_id, 16, 1, 400, 300, 1, 1, f"file:tag:{tag_id:08X}", epoch=1, bridge_addr=addr))
    s = QueueStore(db, clock=lambda: 1000.0)
    s.apply_sync("cred", sync_result([tag_info(t) for t in (own, stale, current, unknown)]))
    with s.db.transaction() as conn:
        conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0")

    assert s.redraw_if_changed(MARKER, "00" * 8) == 3
    assert {v.tag_id for v in s.list_views() if v.dirty} == {own, current, unknown}
    assert drawn_with(s) == MARKER


def test_the_content_key_follows_the_design_version(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = TagPanel(OWNED, 400, 300, 1, 1)
    settings = ScreenSettings()
    created = dt.datetime(2026, 10, 6, 14, 5, tzinfo=dt.UTC)
    cards = [ActiveCard(501, "notification", 40, created, {"v": 1, "kind": "notification", "title": "Hello"})]
    key = content_key("screen", panel, settings, cards, "00" * 8)
    assert content_key("screen", panel, settings, cards, "00" * 8) == key
    monkeypatch.setattr(compose_api, "COMPOSER_VERSION", compose_api.COMPOSER_VERSION + 1)
    assert content_key("screen", panel, settings, cards, "00" * 8) != key


# -- the daemon against the simulator -------------------------------------------------------


@pytest.fixture
def redraws() -> Iterator[list[str]]:
    """The daemon's "redrawing N tag(s) once" log lines, collected on its own logger (``app.tags.hosting.logs``
    stops ``app.tags.runtime`` records from propagating once a test has installed it)."""
    logger = logging.getLogger("app.tags.runtime.daemon.service")
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "redrawing" in record.getMessage():
                lines.append(record.getMessage())

    handler, level = Collect(logging.INFO), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def revisions(rig: Any) -> list[dict[str, Any]]:
    with rig.db() as db, db.reading() as conn:
        return [dict(r) for r in conn.execute("SELECT revision, state, purpose, delivery_ids, layout_digest"
                                              " FROM revisions ORDER BY revision")]


async def settled(rig: Any, timeout: float = 30.0) -> None:
    """The daemon caught up after its start: synced, nothing left to compose, send or report."""
    assert rig.svc is not None
    deadline = time.monotonic() + timeout
    while not await rig.svc.quiescent():
        if time.monotonic() >= deadline:
            raise AssertionError(f"the daemon did not settle within {timeout}s")
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)  # a few more scheduler passes: nothing new may appear


def test_a_restart_with_the_same_design_and_pack_redraws_nothing(make_rig: Any, redraws: list[str]) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()  # a fresh database: records what screens are drawn with, marks nothing
            did = rig.fake.add_job("alice", rig.hw(), title="Drawn once")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="displayed")
            before, refreshes = revisions(rig), rig.sim_tag().stats["refreshes"]
            await rig.stop()
            await rig.start()
            await settled(rig)
            assert revisions(rig) == before
            assert rig.sim_tag().stats["refreshes"] == refreshes
            assert redraws == []
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_a_new_design_redraws_each_tag_once(make_rig: Any, monkeypatch: pytest.MonkeyPatch,
                                            redraws: list[str]) -> None:
    from app.tags.runtime.compose import screen

    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Drawn again in the new look")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="displayed")
            before, refreshes = revisions(rig), rig.sim_tag().stats["refreshes"]
            await rig.stop()

            # The upgrade: the composer draws differently (titles in capitals) and bumps its version. Without
            # the visual change the recomposed layout could equal the old one, which is not sent again.
            original = screen.compose_screen

            def redesigned(panel: TagPanel, cards: list[ActiveCard], *args: Any) -> Any:
                cards = [dataclasses.replace(c, card={**c.card, "title": str(c.card.get("title", "")).upper()})
                         for c in cards]
                return original(panel, cards, *args)

            monkeypatch.setattr(screen, "compose_screen", redesigned)
            monkeypatch.setattr(compose_api, "COMPOSER_VERSION", compose_api.COMPOSER_VERSION + 1)
            await rig.start()
            await rig.wait(lambda: len(revisions(rig)) > len(before) and revisions(rig)[-1]["state"] == "displayed",
                           what="the redrawn screen displayed")
            await settled(rig)
            after = revisions(rig)
            assert after[:-1] == before and len(after) == len(before) + 1  # exactly one new revision
            redraw = after[-1]
            assert redraw["purpose"] == "screen" and redraw["layout_digest"] != before[-1]["layout_digest"]
            assert json.loads(redraw["delivery_ids"]) == json.loads(before[-1]["delivery_ids"]) == [did]
            assert rig.sim_tag().stats["refreshes"] == refreshes + 1
            assert redraws == ["daemon: redrawing 1 tag(s) once (screen design or font pack changed)"]

            await rig.stop()  # the next start finds the same design and pack: nothing more
            await rig.start()
            await settled(rig)
            assert revisions(rig) == after
            assert rig.sim_tag().stats["refreshes"] == refreshes + 1
            assert len(redraws) == 1
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)
