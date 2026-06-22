"""End-to-end tests for the ``overwrite_file`` subtool of the System File tool.

``overwrite_file`` edits an EXISTING text file in place by applying a unified
diff -- the in-place counterpart to ``write_file`` (which recreates a whole
file). Hunks are matched by CONTENT (the ``@@`` header line numbers are only a
hint), so hallucinated/approximate line numbers still apply correctly. These
tests drive the public ``run()`` coroutine against a real temp directory,
mirroring the style of ``test_system_file_grep.py``.

Cross-platform notes (tests must pass on Windows AND CI Linux):
- Seed files with explicit ``newline="\n"`` so on-disk bytes are LF everywhere.
- Read results back with ``Path.read_text`` (universal newlines) and assert
  against ``\n`` strings; the tool writes in text mode like the sibling write
  tools, so line endings may be CRLF on disk but normalise to LF on read.
- Assert on error *codes* and POSIX-style ('/') paths, not OS-specific text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.tools.builtin.system_file import OverwriteFileTool


def _run(tmp_path: Path, args: dict) -> dict:
    tool = OverwriteFileTool(data_dir=str(tmp_path))
    result = asyncio.run(tool.run(args))
    assert result.structured_content is not None
    return result.structured_content


def _seed(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- happy paths -----------------------------------------------------------

def test_simple_replace(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -2,1 +2,1 @@\n-b\n+X"})
    assert "error" not in sc
    assert _read(f) == "a\nX\nc\nd\n"
    assert "Applied 1 hunk(s)" in sc["text"]
    assert sc["_files"][0]["name"] == "a.txt"


def test_replace_with_context(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -1,3 +1,3 @@\n a\n-b\n+X\n c"})
    assert "error" not in sc
    assert _read(f) == "a\nX\nc\nd\n"


def test_context_line_without_leading_space(tmp_path: Path) -> None:
    # Models (and the child dispatcher) often drop the leading space on context
    # lines. Such a line must be kept verbatim, not have its first char stripped.
    f = _seed(tmp_path, "a.txt", "a\nbbb\nccc\nd\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -1,3 +1,3 @@\n-bbb\n+XXX\nccc"})
    assert "error" not in sc
    assert _read(f) == "a\nXXX\nccc\nd\n"  # 'ccc' context intact (not 'cc')


def test_multi_change_single_hunk_unmarked_context(tmp_path: Path) -> None:
    # Mirrors the log-5 case: one hunk renaming several lines, with unmarked
    # context lines between them. Must apply in a single call.
    f = _seed(tmp_path, "c.txt",
              "Sarah: hi\nSteve: one\nSarah: mid\nSteve: two\n")
    diff = ("@@ -2,3 +2,3 @@\n-Steve: one\n+James: one\nSarah: mid\n"
            "-Steve: two\n+James: two")
    sc = _run(tmp_path, {"path": "c.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "Sarah: hi\nJames: one\nSarah: mid\nJames: two\n"
    assert "Applied 1 hunk(s)" in sc["text"]


def test_multi_change_single_hunk_marked_context(tmp_path: Path) -> None:
    # Same change but with proper space-marked context -> still applies (the one
    # marker space is correctly stripped).
    f = _seed(tmp_path, "c.txt",
              "Sarah: hi\nSteve: one\nSarah: mid\nSteve: two\n")
    diff = ("@@ -2,3 +2,3 @@\n-Steve: one\n+James: one\n Sarah: mid\n"
            "-Steve: two\n+James: two")
    sc = _run(tmp_path, {"path": "c.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "Sarah: hi\nJames: one\nSarah: mid\nJames: two\n"


def test_wrong_line_numbers_still_apply(tmp_path: Path) -> None:
    # Header says line 99, but the content match wins (the key robustness case).
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -99,1 +99,1 @@\n-c\n+Z"})
    assert "error" not in sc
    assert _read(f) == "a\nb\nZ\nd\n"


def test_steve_to_james_regression(tmp_path: Path) -> None:
    # The exact scenario from the bug report that motivated this redesign.
    f = _seed(tmp_path, "conversation.txt",
              "Sarah: Hi!\nSteve: Oh, hello! I'm Steve.\n")
    sc = _run(tmp_path, {
        "path": "conversation.txt",
        "diff": ("@@ -2,1 +2,1 @@\n"
                 "-Steve: Oh, hello! I'm Steve.\n"
                 "+James: Oh, hello! I'm James."),
    })
    assert "error" not in sc
    assert _read(f) == "Sarah: Hi!\nJames: Oh, hello! I'm James.\n"


def test_pure_delete(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -2,1 +2,0 @@\n-b"})
    assert "error" not in sc
    assert _read(f) == "a\nc\nd\n"


def test_prepend_via_header(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -0,0 +1,1 @@\n+NEW"})
    assert "error" not in sc
    assert _read(f) == "NEW\na\nb\nc\n"


def test_context_anchored_insert(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -1,2 +1,3 @@\n a\n+NEW\n b"})
    assert "error" not in sc
    assert _read(f) == "a\nNEW\nb\nc\n"


def test_multi_hunk(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\ne\n")
    diff = "@@ -1,1 +1,1 @@\n-a\n+A\n@@ -5,1 +5,1 @@\n-e\n+E"
    sc = _run(tmp_path, {"path": "a.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "A\nb\nc\nd\nE\n"
    assert "Applied 2 hunk(s)" in sc["text"]


def test_multi_hunk_sequential_rematch(tmp_path: Path) -> None:
    # First hunk grows the file; the second still matches by content, not the
    # now-stale header line number.
    f = _seed(tmp_path, "a.txt", "a\nb\nc\nd\n")
    diff = "@@ -1,1 +1,3 @@\n-a\n+A1\n+A2\n+A3\n@@ -4,1 +4,1 @@\n-d\n+D"
    sc = _run(tmp_path, {"path": "a.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "A1\nA2\nA3\nb\nc\nD\n"


def test_headerless_diff(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "-b\n+X"})
    assert "error" not in sc
    assert _read(f) == "a\nX\nc\n"


def test_crlf_diff_tolerated(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -2,1 +2,1 @@\r\n-b\r\n+X"})
    assert "error" not in sc
    assert _read(f) == "a\nX\nc\n"


def test_escaped_quotes_tolerated(tmp_path: Path) -> None:
    # The bug-report case: the model backslash-escaped apostrophes (don\'t) when
    # it quoted the diff. The verbatim match fails, the de-escaped fallback
    # lands, and clean apostrophes are written (no stray backslashes).
    f = _seed(tmp_path, "conversation.txt",
              "Sarah: I don't think we've met.\nJames: I'm James.\n")
    diff = "@@ -2,1 +2,1 @@\n-James: I\\'m James.\n+Steve: I\\'m Steve."
    sc = _run(tmp_path, {"path": "conversation.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "Sarah: I don't think we've met.\nSteve: I'm Steve.\n"


def test_genuine_backslash_quote_preserved(tmp_path: Path) -> None:
    # A file that really contains \' (e.g. source code) is matched verbatim;
    # the de-escape fallback must NOT fire and corrupt it.
    f = _seed(tmp_path, "code.txt", "line one\nval = it\\'s done\n")
    diff = "@@ -2,1 +2,1 @@\n-val = it\\'s done\n+val = CHANGED"
    sc = _run(tmp_path, {"path": "code.txt", "diff": diff})
    assert "error" not in sc
    assert _read(f) == "line one\nval = CHANGED\n"


# --- error paths (assert error code; file must be unchanged) ---------------

def test_context_not_found(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -1,1 +1,1 @@\n-zzz\n+X"})
    assert sc["error"] == "Diff did not apply"
    assert _read(f) == "a\nb\nc\n"  # untouched


def test_ambiguous_context(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "x\nx\nx\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "-x\n+Y"})  # headerless, no anchor
    assert sc["error"] == "Ambiguous diff"
    assert _read(f) == "x\nx\nx\n"  # untouched


def test_ambiguity_resolved_by_header(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "x\nx\nx\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -2,1 +2,1 @@\n-x\n+Y"})
    assert "error" not in sc
    assert _read(f) == "x\nY\nx\n"


def test_missing_path(tmp_path: Path) -> None:
    sc = _run(tmp_path, {"diff": "@@ -1,1 +1,1 @@\n-a\n+A"})
    assert sc["error"] == "Missing parameter"


def test_missing_diff(tmp_path: Path) -> None:
    _seed(tmp_path, "a.txt", "a\nb\n")
    sc = _run(tmp_path, {"path": "a.txt"})
    assert sc["error"] == "Missing parameter"


def test_invalid_diff(tmp_path: Path) -> None:
    _seed(tmp_path, "a.txt", "a\nb\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "This is plain text, no diff."})
    assert sc["error"] == "Invalid diff"


def test_missing_file_not_found(tmp_path: Path) -> None:
    sc = _run(tmp_path, {"path": "nope.txt", "diff": "@@ -1,1 +1,1 @@\n-a\n+A"})
    assert sc["error"] == "Not found"
    assert not (tmp_path / "nope.txt").exists()  # must not create the file


def test_binary_file_rejected(tmp_path: Path) -> None:
    # text/plain MIME but a NUL byte makes _is_binary() true -> rejected.
    (tmp_path / "bin.txt").write_bytes(b"a\x00b\nc\n")
    sc = _run(tmp_path, {"path": "bin.txt", "diff": "@@ -1,1 +1,1 @@\n-a\n+A"})
    assert sc["error"] == "Unsupported file type"


def test_non_text_mime_rejected(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"plain bytes\n")
    sc = _run(tmp_path, {"path": "data.bin", "diff": "@@ -1,1 +1,1 @@\n-x\n+y"})
    assert sc["error"] == "Unsupported file type"


def test_path_traversal_denied(tmp_path: Path) -> None:
    sc = _run(tmp_path, {"path": "../outside.txt", "diff": "@@ -1,1 +1,1 @@\n-a\n+A"})
    assert sc["error"] == "Access denied"


# --- newline preservation --------------------------------------------------

def test_trailing_newline_preserved(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\n")
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -2,1 +2,1 @@\n-b\n+B"})
    assert "error" not in sc
    assert _read(f) == "a\nB\n"


def test_no_trailing_newline_preserved(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb")  # no trailing newline
    sc = _run(tmp_path, {"path": "a.txt", "diff": "@@ -1,1 +1,1 @@\n-a\n+A"})
    assert "error" not in sc
    assert _read(f) == "A\nb"  # still no trailing newline


# --- working-directory override --------------------------------------------

def test_working_directory_override(tmp_path: Path) -> None:
    f = _seed(tmp_path, "a.txt", "a\nb\nc\n")
    # Instantiate against an unused dir; the call-time override must win.
    tool = OverwriteFileTool(data_dir=str(tmp_path / "unused"))
    result = asyncio.run(tool.run({
        "_working_directory": str(tmp_path),
        "path": "a.txt", "diff": "@@ -2,1 +2,1 @@\n-b\n+X",
    }))
    sc = result.structured_content
    assert sc is not None and "error" not in sc
    assert _read(f) == "a\nX\nc\n"
