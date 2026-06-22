"""End-to-end tests for the ``grep_files`` subtool of the System File tool.

``grep_files`` searches file CONTENTS (the counterpart to ``search_files``,
which matches file NAMES). These tests drive the public ``run()`` coroutine
against a real temp directory, mirroring the style of ``test_web_tools.py``.

Cross-platform notes (tests must pass on Windows AND CI Linux):
- Seed files with explicit ``newline="\n"`` so on-disk bytes are LF everywhere;
  the tool reads with universal newlines, so line numbers match on every OS.
- Assert on error *codes* and POSIX-style ('/') paths, not OS-specific text.
- Keep glob/extension casing matching the file names (``fnmatch`` is
  case-insensitive on Windows but case-sensitive on POSIX).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.tools.builtin.system_file import GrepFilesTool


def _seed(root: Path) -> None:
    (root / "a.txt").write_text(
        "alpha line\nBeta LINE\nfind me TODO here\nplain\n",
        encoding="utf-8", newline="\n",
    )
    (root / "b.py").write_text(
        "import os\n# TODO refactor\ndef foo():\n    return 42\n",
        encoding="utf-8", newline="\n",
    )
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.md").write_text(
        "# Title\nword boundary cat catalog\n",
        encoding="utf-8", newline="\n",
    )
    # Leading NUL byte makes _is_binary() detect this as binary on every OS,
    # even though the bytes contain the literal text "TODO".
    (root / "img.bin").write_bytes(b"\x00\x01\x02 TODO \x00 not text")


def _run(tmp_path: Path, args: dict) -> dict:
    tool = GrepFilesTool(data_dir=str(tmp_path))
    result = asyncio.run(tool.run(args))
    assert result.structured_content is not None
    return result.structured_content


# --- 1. basic content match ------------------------------------------------

def test_content_basic_match(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "output_mode": "content"})
    assert "error" not in sc
    assert sc["output_mode"] == "content"
    assert sc["total_matches"] >= 2
    paths = {m["path"] for m in sc["matches"]}
    assert "a.txt" in paths and "b.py" in paths
    assert "img.bin" not in paths  # binary, skipped
    for m in sc["matches"]:
        assert "line" in m and "path" in m


# --- 2. default mode is files_with_matches ---------------------------------

def test_default_mode_is_files_with_matches(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO"})
    assert sc["output_mode"] == "files_with_matches"
    assert "files" in sc and "matches" not in sc
    assert set(sc["files"]) == {"a.txt", "b.py"}
    assert sc["total_files"] == 2


# --- 3. count mode ----------------------------------------------------------

def test_count_mode(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "output_mode": "count"})
    assert sc["output_mode"] == "count"
    assert sum(c["count"] for c in sc["counts"]) == sc["total_matches"]
    assert sc["total_matches"] == 2


# --- 4. case-insensitive ----------------------------------------------------

def test_case_insensitive(tmp_path: Path) -> None:
    _seed(tmp_path)
    insensitive = _run(tmp_path, {
        "pattern": "line", "output_mode": "content", "case_insensitive": True,
        "path": "a.txt",
    })
    matched = {m["line"] for m in insensitive["matches"]}
    assert "alpha line" in matched and "Beta LINE" in matched

    sensitive = _run(tmp_path, {
        "pattern": "line", "output_mode": "content", "path": "a.txt",
    })
    matched_cs = {m["line"] for m in sensitive["matches"]}
    assert "alpha line" in matched_cs and "Beta LINE" not in matched_cs


# --- 5. glob filters (plain + brace expansion) -----------------------------

def test_glob_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "glob": "*.py"})
    assert set(sc["files"]) == {"b.py"}


def test_glob_brace_expansion(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "glob": "*.{py,txt}"})
    assert set(sc["files"]) == {"a.txt", "b.py"}


# --- 6. type filter ---------------------------------------------------------

def test_type_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "type": "py"})
    assert set(sc["files"]) == {"b.py"}


# --- 7. line numbers + context ---------------------------------------------

def test_line_numbers_and_context(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "find me", "output_mode": "content",
        "path": "a.txt", "context": 1,
    })
    assert len(sc["matches"]) == 1
    m = sc["matches"][0]
    assert m["line_number"] == 3
    assert [c["line"] for c in m["before"]] == ["Beta LINE"]
    assert [c["line"] for c in m["after"]] == ["plain"]


def test_before_context_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "find me", "output_mode": "content",
        "path": "a.txt", "before_context": 1, "after_context": 0,
    })
    m = sc["matches"][0]
    assert [c["line_number"] for c in m["before"]] == [2]
    assert "after" not in m


# --- 8. multiline -----------------------------------------------------------

def test_multiline_match(tmp_path: Path) -> None:
    (tmp_path / "ml.txt").write_text(
        "start\nMID\nend\ntail\n", encoding="utf-8", newline="\n",
    )
    on = _run(tmp_path, {
        "pattern": r"start.*end", "output_mode": "content", "multiline": True,
    })
    assert on["total_matches"] == 1
    assert on["matches"][0]["line_number"] == 1

    off = _run(tmp_path, {
        "pattern": r"start.*end", "output_mode": "content",
    })
    assert off["total_matches"] == 0


# --- 9. fixed strings -------------------------------------------------------

def test_fixed_strings(tmp_path: Path) -> None:
    _seed(tmp_path)
    # "foo()" is a regex with a (no-op) group; as a literal it matches def foo().
    sc = _run(tmp_path, {
        "pattern": "foo()", "output_mode": "content",
        "fixed_strings": True, "path": "b.py",
    })
    assert any("def foo()" in m["line"] for m in sc["matches"])


# --- 10. whole word ---------------------------------------------------------

def test_whole_word(tmp_path: Path) -> None:
    _seed(tmp_path)
    ww = _run(tmp_path, {
        "pattern": "cat", "output_mode": "content",
        "whole_word": True, "path": "sub/c.md",
    })
    # the standalone "cat" matches, but "catalog" must not add a second entry
    assert ww["total_matches"] == 1

    control = _run(tmp_path, {
        "pattern": "cat", "output_mode": "count", "path": "sub/c.md",
    })
    assert control["total_matches"] == 1  # one line contains both cat + catalog


# --- 11. invert match -------------------------------------------------------

def test_invert_match(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "TODO", "output_mode": "content",
        "invert_match": True, "path": "a.txt",
    })
    assert all("TODO" not in m["line"] for m in sc["matches"])
    assert sc["total_matches"] == 3  # 4 lines, 1 has TODO


def test_invert_with_multiline_rejected(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "x", "invert_match": True, "multiline": True,
    })
    assert sc["error"] == "Invalid combination"


# --- 12. max_results truncation --------------------------------------------

def test_max_results_truncation(tmp_path: Path) -> None:
    (tmp_path / "hits.txt").write_text(
        "HIT\nHIT\nHIT\nHIT\nHIT\n", encoding="utf-8", newline="\n",
    )
    capped = _run(tmp_path, {
        "pattern": "HIT", "output_mode": "content", "max_results": 2,
    })
    assert len(capped["matches"]) == 2
    assert capped["truncated"] is True
    assert capped["truncation_note"]

    full = _run(tmp_path, {
        "pattern": "HIT", "output_mode": "content", "max_results": 50,
    })
    assert full["truncated"] is False
    assert full["truncation_note"] is None


# --- 13. binary skipping ----------------------------------------------------

def test_binary_skipping(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO"})
    assert "img.bin" not in sc["files"]
    assert sc["files_skipped_binary"] >= 1


# --- 14. no match -----------------------------------------------------------

def test_no_match(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "zzz_nothing_matches", "output_mode": "content"})
    assert "error" not in sc
    assert sc["total_matches"] == 0
    assert sc["matches"] == []


# --- 15. invalid regex ------------------------------------------------------

def test_invalid_regex(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "[unterminated"})
    assert sc["error"] == "Invalid regex"
    assert "pattern" in sc


# --- 16. path traversal -----------------------------------------------------

def test_path_traversal_rejected(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "path": "../../etc"})
    assert sc["error"] == "Access denied"


# --- 17. nonexistent path ---------------------------------------------------

def test_nonexistent_path(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "path": "does/not/exist"})
    assert sc["error"] == "Not found"


# --- 18. single-file path ---------------------------------------------------

def test_single_file_path(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": "TODO", "output_mode": "content", "path": "b.py"})
    assert sc["files_searched"] == 1
    assert all(m["path"] == "b.py" for m in sc["matches"])


# --- 19. oversized skipping (exercises the MAX_GREP_FILE_SIZE var wiring) ---

def test_oversized_skipping(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "TODO",
        "_variables": {"MAX_GREP_FILE_SIZE": "10"},
    })
    assert sc["files_skipped_too_large"] >= 1
    assert sc["files_searched"] == 0


# --- 20. missing pattern + line numbers off --------------------------------

def test_missing_pattern(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {"pattern": ""})
    assert sc["error"] == "Missing parameter"


def test_line_numbers_off(tmp_path: Path) -> None:
    _seed(tmp_path)
    sc = _run(tmp_path, {
        "pattern": "TODO", "output_mode": "content", "show_line_numbers": False,
    })
    assert sc["matches"]
    assert all("line_number" not in m for m in sc["matches"])


# --- 21. only_matching ------------------------------------------------------

def test_only_matching(tmp_path: Path) -> None:
    (tmp_path / "om.txt").write_text(
        "foo bar foo baz\n", encoding="utf-8", newline="\n",
    )
    sc = _run(tmp_path, {
        "pattern": "foo", "output_mode": "content", "only_matching": True,
    })
    # grep -o: one entry per occurrence, each entry is just the match
    assert len(sc["matches"]) == 2
    assert all(m["line"] == "foo" for m in sc["matches"])
