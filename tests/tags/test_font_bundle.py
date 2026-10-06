"""The font asset bundle a release ships (scripts/tags/build_font_bundle.py).

A gateway computer verifies a downloaded bundle against the SHA-256 pinned in
app/tags/runtime/fonts/bundle.json, and CI proves the pin by rebuilding it —
so the archive must be byte-for-byte reproducible: entries sorted, times and
owners zeroed, modes normalised, gzip without a timestamp.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location("build_font_bundle", REPO / "scripts" / "tags" / "build_font_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> None:
    (root / "fonts" / "abc123" / "cache" / "noto").mkdir(parents=True)
    (root / "fonts" / "abc123" / "fontpack.ctfp").write_bytes(b"pack")
    (root / "fonts" / "abc123" / "fontpack.json").write_text('{"pack_id": "abc123"}', encoding="utf-8")
    (root / "fonts" / "abc123" / "cache" / "noto" / "NotoSans.ttf").write_bytes(b"font" * 100)


def test_the_archive_is_reproducible(tmp_path) -> None:
    m = _module()
    first, second = tmp_path / "a", tmp_path / "b"
    _tree(first)
    time.sleep(0.01)
    _tree(second)
    os.utime(second / "fonts" / "abc123" / "fontpack.ctfp", (1, 1))  # different times on disk
    one, two = m._tar_bytes(first), m._tar_bytes(second)
    assert one == two, "the same files give the same bytes, whatever their times"
    with tarfile.open(fileobj=io.BytesIO(one), mode="r:gz") as tar:
        members = tar.getmembers()
    names = [member.name for member in members]
    assert names == sorted(names) and "fonts/abc123/fontpack.ctfp" in names
    assert {member.mtime for member in members} == {0} and {member.uid for member in members} == {0}
    assert {member.mode for member in members if member.isfile()} == {0o644}


def test_the_pin_names_a_published_release_asset() -> None:
    m = _module()
    pin = json.loads((REPO / "app" / "tags" / "runtime" / "fonts" / "bundle.json").read_text(encoding="utf-8"))
    assert pin["schema"] == m.SCHEMA and len(pin["sha256"]) == 64 and pin["size"] > 0
    assert pin["url"] == f"{m.URL_BASE}/{m.release_tag(pin['pack_id'])}/{m.archive_name(pin['pack_id'])}"


def test_only_a_linux_build_is_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    """Windows and macOS builds have another pack id, so --write-lock refuses to run there (before building
    anything); --check and a plain build still run everywhere."""
    m = _module()
    pin = {"schema": m.SCHEMA, "pack_id": "abc123", "profile": "full", "url": "https://example.invalid/a.tar.gz",
           "sha256": "0" * 64, "size": 1}
    built: list[str] = []
    monkeypatch.setattr(m, "build", lambda profile, out_dir: built.append(profile) or dict(pin))
    monkeypatch.setattr(m, "REPO", tmp_path)
    monkeypatch.setattr(m, "LOCK", tmp_path / "bundle.json")
    monkeypatch.setattr(sys, "platform", "win32")
    assert m.main(["--write-lock"]) == 1 and built == [] and not m.LOCK.exists()
    assert "Linux only" in capsys.readouterr().err
    m.LOCK.write_text(json.dumps(pin), encoding="utf-8")
    assert m.main(["--check"]) == 0 and m.main([]) == 0 and built == ["full", "full"]
    monkeypatch.setattr(sys, "platform", "linux")
    m.LOCK.unlink()
    assert m.main(["--write-lock"]) == 0 and json.loads(m.LOCK.read_text(encoding="utf-8")) == pin
