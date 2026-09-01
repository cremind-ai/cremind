"""``search_files`` must not be able to hang the server.

The walk is synchronous and unbounded by nature: a search rooted at the home
directory visits every file the user owns. Two things went wrong at once.

It ran inline in the ``async def run``, so it blocked the event loop — every
other conversation, channel and API request in the process stalled behind it,
and the adapter's tool-call timeout could not fire, because ``wait_for``
cancels at an await point and this coroutine never reached one. And nothing
bounded the traversal itself, so the walk ran to the end of the tree.

Real case: an agent hunting for a filename escalated to searching the whole
home directory and took the server down with it. These pin the offload and the
budget; ``test_sandbox_auto_recovery`` pins the other half (auto-recovery no
longer hands it the home root to walk in the first place).

Style follows ``test_system_file_grep.py``: drive the public ``run()`` against
a real temp tree, assert on codes and POSIX-style paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.tools.builtin import system_file
from app.tools.builtin.system_file import SearchFilesTool


def _seed(root: Path) -> None:
    (root / "report.txt").write_text("x", encoding="utf-8")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    sub = root / "sub"
    sub.mkdir()
    (sub / "photo-2.jpg").write_bytes(b"\xff\xd8\xff")
    (sub / "notes.md").write_text("x", encoding="utf-8")


def _run(tmp_path: Path, args: dict) -> dict:
    tool = SearchFilesTool(data_dir=str(tmp_path))
    result = asyncio.run(tool.run(args))
    assert result.structured_content is not None
    return result.structured_content


# ── it still searches ─────────────────────────────────────────────────────


def test_a_name_match_is_found_in_a_subdirectory(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"query": "photo"})

    assert "error" not in sc
    paths = {r["path"] for r in sc["results"]}
    assert "photo.jpg" in paths
    assert "sub/photo-2.jpg" in paths
    assert sc["truncated"] is False
    assert sc["truncation_note"] is None


def test_the_pattern_and_type_filters_still_apply(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"query": "photo", "pattern": "*.jpg", "type": "file"})

    assert {r["path"] for r in sc["results"]} == {"photo.jpg", "sub/photo-2.jpg"}
    assert all(r["type"] == "file" for r in sc["results"])


def test_a_denied_path_still_reports_access_denied(tmp_path: Path) -> None:
    sc = _run(tmp_path, {"query": "x", "path": str(tmp_path.parent / "elsewhere")})
    assert sc["error"] == "Access denied"


# ── it no longer blocks the event loop ────────────────────────────────────


def test_the_walk_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The whole point: while the walk runs, the loop must still be able to run
    other work. Asserted by racing a plain sleeping task against a walk that
    blocks for longer than it."""
    _seed(tmp_path)

    real_walk = system_file.os.walk

    def _slow_walk(*args, **kwargs):
        import time
        time.sleep(0.3)  # a stand-in for a very large tree
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(system_file.os, "walk", _slow_walk)

    async def _race() -> list[str]:
        order: list[str] = []

        async def _other() -> None:
            await asyncio.sleep(0.05)
            order.append("loop-still-alive")

        async def _search() -> None:
            await SearchFilesTool(data_dir=str(tmp_path)).run({"query": "photo"})
            order.append("search-done")

        await asyncio.gather(_search(), _other())
        return order

    # The sleeping task finishes FIRST — impossible if the walk held the loop.
    assert asyncio.run(_race()) == ["loop-still-alive", "search-done"]


# ── it is bounded ─────────────────────────────────────────────────────────


def test_the_scan_budget_stops_a_runaway_walk(tmp_path: Path, monkeypatch) -> None:
    """Counted per entry VISITED, not per match — a query that matches nothing
    is exactly when a walk runs longest, and that is the case to bound."""
    for i in range(40):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(system_file, "MAX_SCAN_ENTRIES", 10)

    sc = _run(tmp_path, {"query": "nothing-matches-this"})

    assert sc["results"] == []
    assert sc["truncated"] is True
    assert sc["entries_scanned"] <= 11  # stops one past the budget
    assert "budget" in sc["truncation_note"]
    assert "path" in sc["truncation_note"]  # says how to narrow it


def test_hitting_the_result_limit_reads_as_a_limit_not_a_budget(
    tmp_path: Path,
) -> None:
    for i in range(10):
        (tmp_path / f"photo{i}.jpg").write_bytes(b"\xff\xd8\xff")

    sc = _run(tmp_path, {"query": "photo", "max_results": 3})

    assert len(sc["results"]) == 3
    assert sc["truncated"] is True
    assert "Result limit" in sc["truncation_note"]


def test_the_grep_walk_is_bounded_too(tmp_path: Path, monkeypatch) -> None:
    """``MAX_GREP_FILES_SCANNED`` only counts files that pass the name filter,
    so a glob matching nothing walks the whole tree before yielding once."""
    from app.tools.builtin.system_file import GrepFilesTool

    for i in range(40):
        (tmp_path / f"f{i}.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(system_file, "MAX_SCAN_ENTRIES", 5)

    tool = GrepFilesTool(data_dir=str(tmp_path))
    candidates = list(tool._walk_files(str(tmp_path), None, None))

    assert len(candidates) <= 5


@pytest.mark.parametrize("missing", ["", "   "])
def test_an_empty_query_is_still_refused(tmp_path: Path, missing: str) -> None:
    sc = _run(tmp_path, {"query": missing})
    assert sc["error"] == "Missing parameter"
