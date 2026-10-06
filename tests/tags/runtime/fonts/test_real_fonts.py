"""The pinned fonts themselves: needs the verified cache (`cremind tags tools fonts fetch`) or the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tags.runtime.fonts.build import build, plan_build
from app.tags.runtime.fonts.coverage import candidate_faces, check_text, face_cmap
from app.tags.runtime.fonts.fetch import VerificationError, check_font_facts, download, git_blob_sha1
from app.tags.runtime.fonts.fontset import FontSet
from app.tags.runtime.fonts.manifest import (
    check_icon_codepoints,
    load_icon_map,
    load_lock,
    load_manifest,
    parse_codepoints,
)
from app.tags.runtime.fonts.rasterize import freetype_version

REPO = Path(__file__).resolve().parents[4]
MANIFEST = REPO / "fonts" / "manifest.yaml"
SAMPLES = "Tiếng Việt có dấu, Ελληνικά, Русский, مرحبا بالعالم, שלום עולם, สวัสดีครับ, नमस्ते दुनिया"


@pytest.mark.fonts
def test_pinned_files_match_the_manifest(real_cache: Path) -> None:
    m = load_manifest(MANIFEST)
    lk = load_lock(m.lock_path)
    assert check_font_facts(m, {f.key: real_cache / lk.file(f.key).cache for f in m.faces}) == []
    codepoints = parse_codepoints((real_cache / lk.file("material-icons.codepoints").cache).read_text("utf-8"))
    check_icon_codepoints(load_icon_map(m), codepoints)


@pytest.mark.fonts
def test_dev_pack_is_reproducible_and_loads(real_cache: Path, tmp_path: Path) -> None:
    m = load_manifest(MANIFEST)
    if freetype_version() != m.render.freetype:
        pytest.skip(f"FreeType {freetype_version()} is not the pinned {m.render.freetype}")
    plan = plan_build(m, "dev")
    a = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "a", jobs=1)
    b = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "b", jobs=4)
    assert a.pack_id == b.pack_id and a.pack_path.read_bytes() == b.pack_path.read_bytes()
    fs = FontSet.load(a.pack_path, real_cache)
    # dev keeps the old shape (16/24 px, regular only), so the old-pack path stays covered.
    assert [f.face_id for f in fs.faces] == [0, 1, 5, 29, 47, 151]
    assert all(f.regular_face_id is None for f in fs.faces)
    assert check_text(SAMPLES, fs) == ()
    assert check_text("日本 😀", fs) == ("日", "本", "😀")
    assert fs.strike(0, 48).glyph_count == 25 and not fs.has_strike(1, 32)
    assert not fs.has_strike(1, 12) and not fs.has_strike(1, 14)


@pytest.mark.fonts
def test_full_profile_has_small_sizes_and_noto_sans_bold(real_cache: Path, tmp_path: Path) -> None:
    m = load_manifest(MANIFEST)
    full = plan_build(m, "full")
    assert full.text_sizes == (12, 14, 16, 24, 32) and m.face("noto-sans-bold") in full.faces
    plan = plan_build(m, "full", faces=["noto-sans-bold"])  # brings Noto Sans along
    assert [f.face_id for f in plan.faces] == [0, 1, 172]
    r = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "bold", strict_freetype=False)
    fs = FontSet.load(r.pack_path, real_cache)
    sans, bold = fs.face(1), fs.face(172)
    assert (bold.role, bold.weight, bold.regular_face_id, bold.scripts) == ("weight", 700, 1, sans.scripts)
    for size in (12, 14, 16, 24, 32):
        a, b = fs.strike(1, size), fs.strike(172, size)
        assert (a.ascent, a.descent, a.line_height) == (b.ascent, b.descent, b.line_height), size
    assert fs.strike(1, 12).ascent == 13 and fs.strike(1, 14).ascent == 15
    assert face_cmap(bold.path) == face_cmap(sans.path)  # bold can set any text Noto Sans sets
    assert [f.face_id for f in candidate_faces(fs, "Latn")] == [1]  # never a fallback face


@pytest.mark.fonts
def test_cjk_regional_faces_share_bitmaps(real_cache: Path, tmp_path: Path) -> None:
    m = load_manifest(MANIFEST)
    plan = plan_build(m, "full", faces=["noto-sans-sc", "noto-sans-tc", "noto-sans-hk"], sizes=[16])
    r = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "cjk", strict_freetype=False)
    assert r.bitmap_area_size < 0.75 * r.undeduplicated_bitmap_size  # TC and HK share ~88 % of their glyphs
    fs = FontSet.load(r.pack_path, real_cache)
    assert [f.key for f in candidate_faces(fs, "Hani", "zh-Hant-HK")][0] == "noto-sans-hk"
    assert [f.key for f in candidate_faces(fs, "Hani", "zh-TW")][0] == "noto-sans-tc"


@pytest.mark.network
def test_download_verifies_against_the_pinned_blob() -> None:
    m = load_manifest(MANIFEST)
    face = min((f for f in m.faces if f.role == "primary"), key=lambda f: f.file.size)
    try:
        data = download(m.url(face.file), retries=1, timeout=20)
    except VerificationError as exc:
        pytest.skip(f"offline: {exc}")
    assert len(data) == face.file.size and git_blob_sha1(data) == face.file.git_blob_sha1
