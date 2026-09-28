"""The send gate: nothing cached leaves a restarted worker before it is current.

A screen composed before a restart, or a command claimed before it, must wait
for the worker's identity and a fresh content sync; a restore (a new stream)
seen while running closes the gate again until the next sync.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tags.runtime.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(150)


def _revisions(rig: Any) -> list[tuple[str, int]]:
    with rig.db() as db, db.reading() as conn:
        return [(r["state"], r["revision"]) for r in conn.execute("SELECT state, revision FROM revisions")]


def test_a_screen_composed_before_a_restart_waits_for_the_sync(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.crash.arm("revision_persisted")
            await rig.start()
            stale = rig.fake.add_job("alice", rig.hw(), title="Before the restore")
            assert await rig.wait_crash() == "revision_persisted"
            pending = [rev for state, rev in _revisions(rig) if state == "pending"]
            assert pending, "the crash left a composed, unsent screen"
            # Meanwhile Cremind was restored (the job is cancelled there, the stream is new) and is slow to answer.
            rig.fake.restore("alice")
            rig.fake.fail_next["sync"] = [503] * 6
            svc = await rig.start()
            await rig.wait(lambda: rig.fake.fail_next.get("sync") == [], 30, what="the failing syncs")
            assert not svc.gate.is_open
            assert svc.status_snapshot()["gate"]["open"] is False
            assert all(state == "pending" for state, rev in _revisions(rig) if rev in pending), \
                "a cached screen went out before the sync"
            await rig.wait(lambda: svc.gate.is_open, 30, what="the gate to open after a sync")
            fresh = rig.fake.add_job("alice", rig.hw(), title="After the restore")
            await rig.wait(lambda: rig.stage(fresh) == "displayed", 30, what="the new delivery")
            # The job Cremind cancelled never reached the tag: its screen was superseded, never sent.
            assert all(state == "superseded" for state, rev in _revisions(rig) if rev in pending), (pending, _revisions(rig))
            assert rig.fake.terminal_outcomes().get(stale, {"cancelled"}) <= {"cancelled"}

    run_scenario(scenario(), timeout=120)


def test_a_command_claimed_before_a_restart_waits_for_the_gate(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(bridges=2) as rig:
            rig.crash.arm("command_claimed")
            await rig.start()
            command = rig.fake.assign(rig.hw(), rig.bridge_hw(1))
            assert await rig.wait_crash() == "command_claimed"
            rig.fake.fail_next["sync"] = [503] * 4
            svc = await rig.start()
            await rig.wait(lambda: rig.fake.fail_next.get("sync") == [], 30, what="the failing syncs")
            assert rig.fake.commands[command]["status"] == "claimed"  # held while the gate was closed
            await rig.wait(lambda: rig.fake.commands[command]["status"] == "succeeded", 30, what="the command")
            assert svc.gate.is_open

    run_scenario(scenario(), timeout=120)


def test_a_restore_seen_while_running_closes_and_reopens_the_gate(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            await rig.wait(lambda: svc.gate.is_open, 30, what="the gate")
            first = rig.fake.add_job("alice", rig.hw(), title="Before")
            await rig.wait(lambda: rig.stage(first) == "displayed", 30, what="the first delivery")
            opened = svc.gate.opened
            rig.fake.restore("alice")
            await rig.wait(lambda: svc.gate.opened > opened, 30, what="the gate to close and reopen")
            second = rig.fake.add_job("alice", rig.hw(), title="After")
            await rig.wait(lambda: rig.stage(second) == "displayed", 30, what="the delivery after the restore")

    run_scenario(scenario(), timeout=120)
