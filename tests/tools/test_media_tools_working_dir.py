"""Image/audio understanding and the Markdown converter keep to the caller's
working directory.

Each profile's working directory is its own, and every file surface refuses a
path inside another's — the admin included, even when its own (legacy) folder
contains the workspaces root. These three tools read files but used to resolve
them with no profile at all; they now judge the path for the CALLING profile
(``_profile``, adapter-injected), exactly as the ``system_file`` tools do, and
a relative path with no conversation directory resolves against that profile's
own folder rather than the system folder.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import working_dirs as wd
from app.tools.builtin import audio_understanding as au
from app.tools.builtin import image_understanding as iu
from app.tools.builtin import markdown_converter as mc

cfg = importlib.import_module("app.config.settings")


class _Store:
    """The three DynamicConfigStorage methods working_dirs uses."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


@pytest.fixture
def folders(tmp_path: Path, monkeypatch):
    """``admin``'s folder (legacy layout) CONTAINS the workspaces root, which
    holds ``bob``'s default folder — the case where only the ownership rule,
    not the allowed-roots rule, keeps the admin out of bob's files."""
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    admin_dir = tmp_path / "Documents"
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(admin_dir / "workspaces"))
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _Store({"admin": str(admin_dir), "bob": None}))
    wd.invalidate()
    admin = Path(cfg.get_user_working_directory("admin"))
    bob = Path(cfg.get_user_working_directory("bob"))
    assert bob.parent.parent == admin
    for folder in (admin, bob):
        (folder / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n not-a-real-image")
        (folder / "clip.mp3").write_bytes(b"ID3 not-real-audio")
        (folder / "notes.html").write_text("<p>hi</p>", encoding="utf-8")
    yield SimpleNamespace(sysdir=sysdir, admin=admin, bob=bob)
    wd.invalidate()


_LLM = SimpleNamespace(provider_name="fake", model_name="fake-model", model_label="fake-model")


def _image(args, data_dir):
    return asyncio.run(iu.AnalyzeImageTool(data_dir=str(data_dir)).run(
        {"query": "what is this?", "_llm": _LLM, **args}
    )).structured_content


def _audio(args, data_dir):
    return asyncio.run(au.AnalyzeAudioTool(data_dir=str(data_dir)).run(
        {"query": "transcribe", "_llm": _LLM, **args}
    )).structured_content


@pytest.fixture
def no_model(monkeypatch):
    # The capability gate runs right AFTER the path is resolved, so "not
    # supported" proves the path was accepted without any model call.
    monkeypatch.setattr(iu, "model_supports_vision", lambda *a, **k: False)
    monkeypatch.setattr(au, "model_supports_audio", lambda *a, **k: False)


def _refused(result) -> bool:
    return result.get("error") == "Access denied" and "another profile" in result.get("message", "")


# ── image / audio understanding ───────────────────────────────────────────


@pytest.mark.parametrize("run, name, accepted", [
    (_image, "pic.png", "VisionNotSupported"),
    (_audio, "clip.mp3", "AudioNotSupported"),
])
def test_admin_cannot_reach_a_profile_folder_inside_its_own(folders, no_model, run, name, accepted):
    rel = f"workspaces/bob/{name}"
    got = run({"path": rel, "_profile": "admin", "_working_directory": str(folders.admin)}, folders.sysdir)
    assert _refused(got), got
    got = run({"path": str(folders.bob / name), "_profile": "admin",
               "_working_directory": str(folders.admin)}, folders.sysdir)
    assert _refused(got), got
    # Its own file right beside them stays reachable.
    got = run({"path": name, "_profile": "admin", "_working_directory": str(folders.admin)}, folders.sysdir)
    assert got.get("error") == accepted, got


@pytest.mark.parametrize("run, name, accepted", [
    (_image, "pic.png", "VisionNotSupported"),
    (_audio, "clip.mp3", "AudioNotSupported"),
])
def test_a_profile_reads_its_own_folder_and_nobody_elses(folders, no_model, run, name, accepted):
    got = run({"path": name, "_profile": "bob", "_working_directory": str(folders.bob)}, folders.sysdir)
    assert got.get("error") == accepted, got
    got = run({"path": str(folders.admin / name), "_profile": "bob",
               "_working_directory": str(folders.bob)}, folders.sysdir)
    assert _refused(got), got


@pytest.mark.parametrize("run, name, accepted", [
    (_image, "pic.png", "VisionNotSupported"),
    (_audio, "clip.mp3", "AudioNotSupported"),
])
def test_without_a_conversation_directory_the_profiles_own_folder_is_the_base(
    folders, no_model, run, name, accepted,
):
    # No `_working_directory`: the relative path resolves in bob's folder, not
    # in the system folder the tool was built with (which holds no such file).
    got = run({"path": name, "_profile": "bob"}, folders.sysdir)
    assert got.get("error") == accepted, got


@pytest.mark.parametrize("run, name, accepted", [
    (_image, "pic.png", "VisionNotSupported"),
    (_audio, "clip.mp3", "AudioNotSupported"),
])
def test_the_callers_own_manual_pages_open_and_nobody_elses(
    folders, no_model, monkeypatch, run, name, accepted,
):
    # The manual-pages rule needs the caller: with no profile passed (the old
    # call) every profile's pages were refused, its own included.
    import app.cremind_documents.paths as doc_paths

    uids = {"admin": "a" * 32, "bob": "b" * 32}
    monkeypatch.setattr(doc_paths, "resolve_profile_uid", lambda p: uids.get(p))
    pages = folders.sysdir / "storage" / "cremind_documents" / "profiles" / uids["bob"]
    pages.mkdir(parents=True)
    (pages / name).write_bytes((folders.bob / name).read_bytes())
    got = run({"path": str(pages / name), "_profile": "bob",
               "_working_directory": str(folders.bob)}, folders.sysdir)
    assert got.get("error") == accepted, got
    got = run({"path": str(pages / name), "_profile": "admin",
               "_working_directory": str(folders.admin)}, folders.sysdir)
    assert got.get("error") == "Access denied" and "manual" in got.get("message", ""), got


# ── Markdown converter ────────────────────────────────────────────────────


@pytest.fixture
def converter(monkeypatch):
    class _Converter:
        def convert(self, path):
            return SimpleNamespace(text_content=f"# converted {os.path.basename(path)}")

    monkeypatch.setattr(mc, "_get_markitdown", lambda: _Converter())


def _convert(args):
    return asyncio.run(mc.ConvertToMarkdownTool(data_dir="unused").run(
        {"profile": "whoever", **args}
    )).structured_content


def test_converter_refuses_another_profiles_folder_to_the_admin(folders, converter):
    got = _convert({"source_path": "workspaces/bob/notes.html", "_profile": "admin",
                    "_working_directory": str(folders.admin)})
    assert _refused(got), got
    assert not (folders.bob / "markdown_files").exists()
    # Nor may it write its output there.
    got = _convert({"source_path": "notes.html", "output_path": "workspaces/bob/out.md",
                    "_profile": "admin", "_working_directory": str(folders.admin)})
    assert _refused(got), got
    assert not (folders.bob / "out.md").exists()
    # Its own file converts.
    got = _convert({"source_path": "notes.html", "_profile": "admin",
                    "_working_directory": str(folders.admin)})
    assert "error" not in got, got
    assert (folders.admin / "markdown_files" / "notes.md").is_file()


def test_converter_uses_the_callers_folder_not_the_model_supplied_profile(folders, converter):
    # `profile` is a model-filled argument; `_profile` is who is calling.
    got = _convert({"source_path": "notes.html", "profile": "admin", "_profile": "bob"})
    assert "error" not in got, got
    assert (folders.bob / "markdown_files" / "notes.md").is_file()
    assert not (folders.admin / "markdown_files").exists()
