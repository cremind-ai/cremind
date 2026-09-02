"""Boot ordering that delivery depends on, pinned as text.

Two orderings matter and neither can be observed from a unit test, because boot
is one long coroutine nested inside ``main()``:

* the undelivered-results sweep must run AFTER the channel adapters start. A
  result recovered at boot belongs to the conversation that registered the rule,
  and if that is a Telegram DM or a platform group its continuation turn needs a
  live adapter to carry the answer out. Before step 11 the channel registry does
  not even exist, so the result would reach the web UI and nowhere else.

* the mid-turn message sweep must NOT move with it. It releases rows that a live
  turn would otherwise fold in, and those two paths share no lock — running it
  once traffic is flowing would answer the same message twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from app import server  # noqa: E402


def _boot_block() -> str:
    """The body of ``boot_storage_and_post_storage``.

    Read as text rather than via ``inspect``: the function is nested inside
    ``main()``, so it is not reachable as a module attribute.
    """
    src = Path(server.__file__).read_text(encoding="utf-8")
    start = src.index("async def boot_storage_and_post_storage")
    end = src.index("\ndef _build_spa_components", start)
    return src[start:end]


def test_the_delivery_sweep_runs_after_the_channel_adapters() -> None:
    block = _boot_block()
    assert block.count("sweep_undelivered()") == 1
    assert block.index("start_all_enabled()") < block.index("await sweep_undelivered()")


def test_the_delivery_sweep_still_runs_after_crash_recovery() -> None:
    """Recovery is what makes an interrupted run terminal, hence deliverable."""
    block = _boot_block()
    assert block.index("recover_after_restart()") < block.index("await sweep_undelivered()")


def test_the_mid_turn_message_sweep_did_not_move() -> None:
    """It has no lock against a live turn, so it must finish before traffic."""
    block = _boot_block()
    assert block.count("sweep_stranded_mid_turn_messages()") == 1
    assert (
        block.index("await sweep_stranded_mid_turn_messages()")
        < block.index("start_all_enabled()")
    )


def test_the_timeout_sweep_still_starts_before_the_adapters() -> None:
    """Safe because its first tick is a sweep interval away, not immediate."""
    block = _boot_block()
    assert (
        block.index("get_task_timeout_manager().start(loop)")
        < block.index("start_all_enabled()")
    )


def test_the_delivery_sweep_cannot_break_boot() -> None:
    """Best-effort, like every other step: a failure must not stop the server."""
    block = _boot_block()
    tail = block[block.index("await sweep_undelivered()"):]
    assert "except Exception" in tail[:400]
