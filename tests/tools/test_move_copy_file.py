"""End-to-end tests for the ``move_file`` and ``copy_file`` subtools.

These tools were added to fix the bug where an uploaded file's ABSOLUTE path
(``~/.cremind/<profile>/uploads_tmp/<id>/<name>``) could not be moved into the
user's working directory: the agent had no move/copy op at all, and the path
descriptions pushed it to relativize the absolute path (stripping the
home/drive prefix), which then 404'd under the working directory.

The tests drive the public ``run()`` coroutine against real temp dirs, mirroring
the style of ``test_system_file_overwrite.py``. ``_allowed_roots`` (via
``build_system_env``) is patched to be deterministic so tests don't depend on
the host's real ``~/.cremind`` / ``~/Documents``.

Cross-platform: assert on error *codes* and POSIX-style ('/') paths, seed binary
content with ``write_bytes`` and compare bytes exactly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.tools.builtin.system_file import (
    CopyFileTool,
    GetFileInfoTool,
    GrepFilesTool,
    ListFilesTool,
    MoveFileTool,
    OverwriteFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
    get_tools,
)


@pytest.fixture(autouse=True)
def _isolate_allowed_roots(monkeypatch):
    """Default: only the tool's ``data_dir`` is an allowed root.

    ``build_system_env`` returns no extra roots, so moves/copies are bounded to
    the test's working directory and are independent of the real host dirs.
    Tests that need an extra allowed root re-patch this within the test.
    """
    monkeypatch.setattr(
        "app.config.system_vars.build_system_env",
        lambda profile=None: {},
    )


def _run(tool_cls, data_dir: Path, args: dict) -> dict:
    tool = tool_cls(data_dir=str(data_dir))
    result = asyncio.run(tool.run(dict(args)))
    assert result.structured_content is not None
    return result.structured_content


def _seed(path: Path, text: str = "hello\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- move: happy paths ------------------------------------------------------

def test_move_into_directory_keeps_basename(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd / "file.txt", "data\n")
    (cwd / "sub").mkdir()
    sc = _run(MoveFileTool, cwd, {"source_path": "file.txt", "destination_path": "sub"})
    assert "error" not in sc
    assert (cwd / "sub" / "file.txt").exists()
    assert not (cwd / "file.txt").exists()
    assert sc["_files"][0]["name"] == "file.txt"


def test_move_renames_when_dest_is_file_path(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd / "a.txt", "x\n")
    sc = _run(MoveFileTool, cwd, {"source_path": "a.txt", "destination_path": "b.txt"})
    assert "error" not in sc
    assert (cwd / "b.txt").read_text(encoding="utf-8") == "x\n"
    assert not (cwd / "a.txt").exists()


def test_move_binary_file_roundtrip(tmp_path: Path) -> None:
    cwd = tmp_path
    blob = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\x00binary"
    (cwd / "img.png").write_bytes(blob)
    (cwd / "out").mkdir()
    sc = _run(MoveFileTool, cwd, {"source_path": "img.png", "destination_path": "out"})
    assert "error" not in sc
    assert (cwd / "out" / "img.png").read_bytes() == blob  # bytes preserved exactly
    assert not (cwd / "img.png").exists()


def test_move_creates_missing_parent_dirs(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd / "a.txt", "y\n")
    sc = _run(MoveFileTool, cwd, {"source_path": "a.txt", "destination_path": "new/deep/x.txt"})
    assert "error" not in sc
    assert (cwd / "new" / "deep" / "x.txt").read_text(encoding="utf-8") == "y\n"


# --- move: error paths ------------------------------------------------------

def test_move_missing_source_returns_not_found(tmp_path: Path) -> None:
    sc = _run(MoveFileTool, tmp_path, {"source_path": "nope.txt", "destination_path": "x.txt"})
    assert sc["error"] == "Not found"


def test_move_missing_params(tmp_path: Path) -> None:
    sc = _run(MoveFileTool, tmp_path, {"source_path": "", "destination_path": "x.txt"})
    assert sc["error"] == "Missing parameter"


def test_move_same_file_rejected(tmp_path: Path) -> None:
    _seed(tmp_path / "a.txt", "z\n")
    sc = _run(MoveFileTool, tmp_path, {"source_path": "a.txt", "destination_path": "a.txt"})
    assert sc["error"] == "Same file"


def test_move_existing_dest_requires_overwrite(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd / "a.txt", "AAA\n")
    _seed(cwd / "b.txt", "BBB\n")
    blocked = _run(MoveFileTool, cwd, {"source_path": "a.txt", "destination_path": "b.txt"})
    assert blocked["error"] == "Destination exists"
    assert (cwd / "a.txt").exists()  # untouched

    ok = _run(MoveFileTool, cwd, {
        "source_path": "a.txt", "destination_path": "b.txt", "overwrite": True,
    })
    assert "error" not in ok
    assert (cwd / "b.txt").read_text(encoding="utf-8") == "AAA\n"
    assert not (cwd / "a.txt").exists()


def test_move_outside_allowed_roots_rejected(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _seed(cwd / "a.txt", "secret\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    sc = _run(MoveFileTool, cwd, {
        "source_path": "a.txt", "destination_path": str(outside / "leak.txt"),
    })
    assert sc["error"] == "Access denied"
    assert (cwd / "a.txt").exists()  # source untouched on a rejected move


def test_move_absolute_source_under_allowed_root(tmp_path: Path, monkeypatch) -> None:
    """Regression test for the reported bug: an ABSOLUTE upload path (with
    spaces) under the per-profile system-dir root moves into the working dir."""
    sysdir = tmp_path / "system"
    profile = "admin"
    monkeypatch.setattr(
        "app.config.system_vars.build_system_env",
        lambda p=None: {"CREMIND_SYSTEM_DIR": str(sysdir)},
    )
    cwd = tmp_path / "Documents"
    cwd.mkdir()
    upload = sysdir / profile / "uploads_tmp" / "conv1" / "Screenshot a b c.png"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"\x89PNG payload")

    sc = _run(MoveFileTool, cwd, {
        "_profile": profile,
        "source_path": str(upload),        # absolute, verbatim — must NOT be relativized
        "destination_path": str(cwd),      # existing directory
    })
    assert "error" not in sc
    moved = cwd / "Screenshot a b c.png"
    assert moved.read_bytes() == b"\x89PNG payload"
    assert not upload.exists()


# --- copy -------------------------------------------------------------------

def test_copy_leaves_source_and_duplicates(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd / "a.txt", "dup\n")
    sc = _run(CopyFileTool, cwd, {"source_path": "a.txt", "destination_path": "b.txt"})
    assert "error" not in sc
    assert (cwd / "a.txt").read_text(encoding="utf-8") == "dup\n"  # original kept
    assert (cwd / "b.txt").read_text(encoding="utf-8") == "dup\n"


def test_copy_directory_rejected(tmp_path: Path) -> None:
    cwd = tmp_path
    (cwd / "adir").mkdir()
    (cwd / "dest").mkdir()
    sc = _run(CopyFileTool, cwd, {"source_path": "adir", "destination_path": "dest"})
    assert sc["error"] == "Unsupported"


# --- guards: descriptions + registration ------------------------------------

def test_path_params_advertise_absolute_paths() -> None:
    """Every path-style param must tell the model absolute paths are accepted and
    must be passed verbatim — this is what stops the relativize-and-404 bug."""
    checks = {
        GetFileInfoTool: ["path"],
        ReadFileTool: ["path"],
        WriteFileTool: ["path"],
        OverwriteFileTool: ["path"],
        ListFilesTool: ["path"],
        SearchFilesTool: ["path"],
        GrepFilesTool: ["path"],
        MoveFileTool: ["source_path", "destination_path"],
        CopyFileTool: ["source_path", "destination_path"],
    }
    for tool_cls, params in checks.items():
        props = tool_cls.parameters["properties"]
        for param in params:
            desc = props[param]["description"]
            assert "absolute" in desc.lower(), f"{tool_cls.__name__}.{param}"
            assert "EXACTLY" in desc, f"{tool_cls.__name__}.{param}"


def test_move_and_copy_registered() -> None:
    names = {t.name for t in get_tools({})}
    assert "move_file" in names
    assert "copy_file" in names
