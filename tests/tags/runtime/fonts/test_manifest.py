"""Manifest, icon map and lock validation (fonts/manifest.yaml, fonts/icons.yaml, manifest.lock.json)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.tags.runtime.fonts.fetch import VerificationError, git_blob_sha1, lock, verify_cached
from app.tags.runtime.fonts.manifest import (
    ManifestError,
    check_lock_current,
    load_icon_map,
    load_lock,
    load_manifest,
    parse_manifest,
)
from app.tags.runtime.protocol.ids import Icon

REPO = Path(__file__).resolve().parents[4]


def test_repository_manifest_is_valid() -> None:
    m = load_manifest(REPO / "fonts" / "manifest.yaml")
    assert m.icon_face.face_id == 0 and m.icon_face.key == "material-icons"
    assert [f.face_id for f in m.faces] == sorted({f.face_id for f in m.faces})
    assert len(m.faces) == 173
    assert {f.role for f in m.faces} == {"icons", "primary", "supplement", "cjk-region", "optional", "emoji", "weight"}
    cjk = {f.key: f.languages for f in m.faces if f.role == "cjk-region"}
    assert set(cjk) == {"noto-sans-sc", "noto-sans-tc", "noto-sans-hk", "noto-sans-jp", "noto-sans-kr"}
    assert all("Hani" in m.face(k).scripts for k in cjk)
    assert m.face("noto-emoji").variations == (("wght", 400.0),)
    assert m.face("noto-sans-arabic").rtl and m.face("noto-sans-hebrew").rtl and not m.face("noto-sans").rtl
    assert m.face("noto-nastaliq-urdu").role == "optional"
    assert {f.license for f in m.faces} == {"OFL-1.1", "Apache-2.0"}
    bold, sans = m.face("noto-sans-bold"), m.face("noto-sans")
    assert (bold.face_id, bold.role, bold.weight, bold.regular, bold.scripts) == (172, "weight", 700, "noto-sans",
                                                                                  sans.scripts)
    assert bold.pack_name == "Noto Sans Bold 2.015" != sans.pack_name
    assert (bold.copyright, bold.trademark, bold.license) == (sans.copyright, sans.trademark, sans.license)
    assert [f.key for f in m.faces if f.role == "weight"] == ["noto-sans-bold"]
    assert m.profiles["full"].faces is None and m.profiles["full"].text_sizes == (12, 14, 16, 24, 32)
    # dev stays 16/24 px regular: the old-pack path keeps its coverage.
    assert m.profiles["dev"].faces is not None and "noto-sans-thai" in m.profiles["dev"].faces
    assert "noto-sans-bold" not in m.profiles["dev"].faces and m.profiles["dev"].text_sizes == (16, 24)


def test_repository_icon_map_matches_the_spec() -> None:
    icons = load_icon_map(load_manifest(REPO / "fonts" / "manifest.yaml"))
    assert [(i.id, i.name) for i in icons] == [(i.value, i.name.lower()) for i in Icon]
    by_name = {i.name: i for i in icons}
    assert by_name["battery_low"].material == "battery_alert" and by_name["battery_low"].note
    assert by_name["hourglass"].material == "hourglass_empty"


def test_repository_lock_is_current() -> None:
    m = load_manifest(REPO / "fonts" / "manifest.yaml")
    lk = load_lock(m.lock_path)
    check_lock_current(m, lk)
    assert lk.manifest_id == hashlib.sha256(m.lock_path.read_bytes()).digest()[:8]
    assert b"\r" not in lk.raw


def test_git_blob_sha1() -> None:
    assert git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def _doc(repo: Any) -> dict[str, Any]:
    return yaml.safe_load(repo.manifest.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(schema="other"), "schema"),
    (lambda d: d["faces"][1].update(face_id=0), "face_id"),
    (lambda d: d["faces"][0].update(face_id=9), "face_id 0"),
    (lambda d: d["faces"][1].update(key="test-sans-x", git_blob_sha1="abc"), "git_blob_sha1"),
    (lambda d: d["faces"][1].update(scripts=["latin"]), "ISO 15924"),
    (lambda d: d["faces"][1].update(role="main"), "role"),
    (lambda d: d["faces"][1].update(hinting="light"), "hinting"),
    (lambda d: d["faces"][1].update(license="GPL"), "license"),
    (lambda d: d["faces"][2].update(path=d["faces"][3]["path"]), "cache file"),
    (lambda d: d["profiles"]["dev"].update(text_sizes=[20]), "sizes"),
    (lambda d: d["profiles"]["dev"].update(faces=["test-sans"]), "icon face"),
    (lambda d: d["profiles"]["dev"].update(faces=["test-icons", "nope"]), "unknown faces"),
    (lambda d: d["sources"]["test"].update(commit="main"), "commit"),
    (lambda d: d["render"]["hinting"].update(cff="light"), "render.hinting"),
    # Weight faces (faces[4] is test-sans-bold, the bold of test-sans).
    (lambda d: d["faces"][4].pop("regular"), "missing 'regular'"),
    (lambda d: d["faces"][4].update(regular="nope"), "not in the manifest"),
    (lambda d: d["faces"][4].update(regular="test-icons"), "only a text face"),
    (lambda d: d["faces"][4].update(weight=400), "other than 400"),
    (lambda d: d["faces"][4].update(weight=750), "steps of 100"),
    (lambda d: d["faces"][4].update(weight=1000), "steps of 100"),
    (lambda d: d["faces"][4].update(scripts=["Latn", "Hani"]), r"\['Hani'\] are not scripts of its regular"),
    (lambda d: d["faces"][1].update(weight=700), "only role 'weight'"),
    (lambda d: d["faces"][2].update(regular="test-sans"), "only role 'weight'"),
    (lambda d: d["faces"].append(dict(d["faces"][4], face_id=5, key="test-sans-bold-2", path="sans/Bold2.ttf")),
     "already has a weight 700 face"),
    (lambda d: d["faces"].append(dict(d["faces"][4], face_id=5, key="test-sans-black", path="sans/Black.ttf",
                                      weight=900, regular="test-sans-bold")), "only a text face"),
    (lambda d: d["profiles"]["dev"].update(faces=["test-icons", "test-sans-bold"]), "regular face in the profile"),
    (lambda d: d["faces"][1].update(role="optional"), "regular face in the profile"),  # `all` leaves it out
])
def test_invalid_manifests(repo: Any, mutate: Any, message: str) -> None:
    doc = copy.deepcopy(_doc(repo))
    mutate(doc)
    with pytest.raises(ManifestError, match=message):
        parse_manifest(doc, repo.manifest)


def test_text_sizes_follow_the_contract(repo: Any) -> None:
    """FONT_SIZES (protocol/spec.yaml) allows 12 and 14 px text strikes since contract 0.3.0; 20 is still no size."""
    doc = _doc(repo)
    doc["profiles"]["dev"]["text_sizes"] = [16, 12, 14]
    assert parse_manifest(doc, repo.manifest).profiles["dev"].text_sizes == (12, 14, 16)
    doc["profiles"]["dev"]["text_sizes"] = [12, 20]
    with pytest.raises(ManifestError, match=r"sizes \[20\]"):
        parse_manifest(doc, repo.manifest)


def test_lock_is_deterministic_and_detects_staleness(repo: Any) -> None:
    m = load_manifest(repo.manifest)
    first = m.lock_path.read_bytes()
    assert lock(m, repo.cache).raw == first
    doc = _doc(repo)
    doc["faces"][1]["size"] += 1
    stale = parse_manifest(doc, repo.manifest)
    with pytest.raises(ManifestError, match="out of date"):
        check_lock_current(stale, load_lock(m.lock_path))


def test_lock_rejects_a_file_that_is_not_the_pinned_blob(repo: Any, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(repo)
    doc["faces"][1]["git_blob_sha1"] = "0" * 40
    m = parse_manifest(doc, tmp_path / "manifest.yaml")
    fetched: list[str] = []

    def fake_download(url: str, **_: Any) -> bytes:
        fetched.append(url)
        return (repo.cache / "test" / "TestSans-Regular.ttf").read_bytes()

    # The cached copy no longer matches the pin, so lock downloads it again; the download fails the pin too.
    monkeypatch.setattr("app.tags.runtime.fonts.fetch.download", fake_download)
    with pytest.raises(VerificationError, match="git blob"):
        lock(m, repo.cache, workers=1)
    assert fetched == ["https://example.invalid/0123456789abcdef0123456789abcdef01234567/sans/TestSans-Regular.ttf"]


def test_lock_rejects_wrong_font_facts(repo: Any, tmp_path: Path) -> None:
    doc = _doc(repo)
    doc["faces"][1]["num_glyphs"] += 1
    doc["faces"][1]["copyright"] = "someone else"
    m = parse_manifest(doc, tmp_path / "manifest.yaml")
    with pytest.raises(VerificationError, match="num_glyphs.*\n.*copyright"):
        lock(m, repo.cache)


def test_verify_cached_detects_tampering(repo: Any, tmp_path: Path) -> None:
    m = load_manifest(repo.manifest)
    lk = load_lock(m.lock_path)
    assert set(verify_cached(lk, repo.cache, ["test-sans"])) == {"test-sans"}
    bad = tmp_path / "cache"
    (bad / "test").mkdir(parents=True)
    src = repo.cache / lk.file("test-sans").cache
    (bad / lk.file("test-sans").cache).write_bytes(src.read_bytes() + b"\0")
    with pytest.raises(VerificationError, match="SHA-256"):
        verify_cached(lk, bad, ["test-sans"])
    with pytest.raises(VerificationError, match="missing"):
        verify_cached(lk, bad, ["test-icons"])
