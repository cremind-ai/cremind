"""`cremind tags tools preview` with the local packs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from app.tags.runtime.cli.main import app

pytestmark = pytest.mark.fonts
runner = CliRunner()
CACHE = Path(__file__).resolve().parents[4] / "fonts" / "cache"


def _run(fonts: Any, *args: str) -> Any:
    return runner.invoke(app, ["preview", *args, "--pack", str(fonts.pack_path), "--cache", str(CACHE)])


def test_preview_text(fonts: Any, tmp_path: Path) -> None:
    out = tmp_path / "t.png"
    r = _run(fonts, "text", "Tiếng Việt مرحبا 123 שלום\\nสวัสดีครับ 你好", "--size", "24", "--width", "300",
             "--lang", "vi", "--out", str(out), "--scale", "2")
    assert r.exit_code == 0, r.output
    assert "faces [1, 5, 47, 151, 166]" in r.output and "unsupported" not in r.output
    assert Image.open(out).width == 2 * 308
    r = _run(fonts, "text", "A\U00020000", "--out", str(tmp_path / "u.png"))
    assert r.exit_code == 0 and "U+20000" in r.output
    r = _run(fonts, "text", "x", "--dir", "sideways")
    assert r.exit_code == 1


JOB = {"delivery_id": 501, "kind": "needs_input", "priority": 90, "created_at": "2026-09-27T07:00:00Z",
       "card": {"v": 1, "kind": "needs_input", "severity": "attention", "icon": "help",
                "title": "Approve deployment?", "body": "Release planning", "lang": "en",
                "ts": "2026-09-27T07:00:00Z", "progress": None, "link": None}}


def test_preview_card_screen_identify(fonts: Any, tmp_path: Path) -> None:
    card_file = tmp_path / "card.json"
    card_file.write_text(json.dumps(JOB), encoding="utf-8")
    out = tmp_path / "card.png"
    r = _run(fonts, "card", str(card_file), "--panel", "bwr", "--now", "2026-09-27T07:05:00Z", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "400x300 rotation 0" in r.output  # bw / bwr: 400x300, unturned, unless the options say otherwise
    assert "shown deliveries [501]" in r.output and Image.open(out).mode == "P"

    doc = {"settings": {"language": "vi", "timezone": "Asia/Ho_Chi_Minh", "show_excerpts": True},
           "jobs": [JOB, {**JOB, "delivery_id": 502, "priority": 40, "kind": "notification",
                          "card": {**JOB["card"], "kind": "notification", "title": "Thông báo mới"}}]}
    screen_file = tmp_path / "screen.json"
    screen_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "screen.png"
    r = _run(fonts, "screen", str(screen_file), "--rotation", "1", "--now", "2026-09-27T07:05:00Z",
             "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "300x400 rotation 1" in r.output and "shown deliveries [501, 502]" in r.output
    assert Image.open(out).size == (300, 400)
    r = _run(fonts, "card", str(screen_file))
    assert r.exit_code == 1 and "2 cards" in r.output

    out = tmp_path / "id.png"
    r = _run(fonts, "identify", "--tag-id", "CAFE0001", "--out", str(out))
    assert r.exit_code == 0 and out.is_file()
    assert "400x300 rotation 0" in r.output and Image.open(out).size == (400, 300)


def test_preview_a_hardware_panel_by_name(fonts: Any, tmp_path: Path) -> None:
    """``--panel hema213`` is the 2.13" Hema: native 128x250, black/white/red, read landscape (rotation 3) — a
    250x128 three-colour preview from every command; the options still override the profile."""
    card_file = tmp_path / "card.json"
    card_file.write_text(json.dumps(JOB), encoding="utf-8")
    runs = {"card": ("card", str(card_file), "--now", "2026-09-27T07:05:00Z"),
            "screen": ("screen", str(card_file), "--now", "2026-09-27T07:05:00Z"),
            "identify": ("identify", "--tag-id", "D1F06B9A", "--name", "Hema")}
    for name, args in runs.items():
        out = tmp_path / f"{name}.png"
        r = _run(fonts, *args, "--panel", "hema213", "--out", str(out))
        assert r.exit_code == 0, (name, r.output)
        assert "250x128 rotation 3" in r.output, (name, r.output)
        image = Image.open(out)
        assert (image.mode, image.size) == ("P", (250, 128)), name
    out = tmp_path / "portrait.png"
    r = _run(fonts, "screen", str(card_file), "--panel", "hema213", "--rotation", "0", "--out", str(out))
    assert r.exit_code == 0 and "128x250 rotation 0" in r.output and Image.open(out).size == (128, 250)
    r = _run(fonts, "identify", "--panel", "unverified", "--out", str(tmp_path / "x.png"))
    assert r.exit_code == 1 and "no verified profile" in r.output
    r = _run(fonts, "identify", "--panel", "hema214", "--out", str(tmp_path / "x.png"))
    assert r.exit_code == 1 and "unknown panel" in r.output and "hema213" in r.output


SAMPLE_SCREENS = ("landscape-bw", "landscape-bwr-excerpts-qr", "portrait-bwr", "landscape-arabic-ui",
                  "portrait-progress", "empty", "hema-bwr", "hema-bw", "hema-portrait-bwr", "hema-test-card",
                  "hema-error", "hema-dense", "hema-vietnamese", "s-264x176-bwr", "landscape-bwr-dense",
                  "small-296x128-bw", "large-800x480-bwr", "identify", "identify-hema", "setup-code-hema",
                  "setup-code-landscape")


def test_preview_samples_with_the_dev_pack(dev_fonts: Any, tmp_path: Path) -> None:
    r = _run(dev_fonts, "samples", "--out", str(tmp_path / "s"), "--scale", "2")
    assert r.exit_code == 0, r.output
    assert "5 faces (5 drawn by their own face)" in r.output and f"{len(SAMPLE_SCREENS)} screens" in r.output
    summary = json.loads((tmp_path / "s" / "summary.json").read_text(encoding="utf-8"))
    assert [f["face_id"] for f in summary["faces"]] == [1, 5, 29, 47, 151]
    assert tuple(s["name"] for s in summary["screens"]) == SAMPLE_SCREENS
    for name in SAMPLE_SCREENS:
        assert (tmp_path / "s" / f"screen-{name}.png").is_file(), name
    screens = {s["name"]: s for s in summary["screens"]}
    hema = screens["hema-bwr"]
    assert hema["panel"] == {"width": 128, "height": 250, "rotation": 3, "planes": 2, "logical": "250x128",
                             "class": "XS"}
    assert hema["uses_red"] and not screens["hema-bw"]["uses_red"] and hema["delivery_ids"][0] == 501
    assert set(hema["sizes"]) <= {16, 24} and hema["faces"] and hema["glyphs"] > 0 and hema["commands"] > 0
    # Rendered at the scale asked for, however large (no fall-back to scale 1).
    assert Image.open(tmp_path / "s" / "screen-large-800x480-bwr.png").size == (1600, 960)
    assert Image.open(tmp_path / "s" / "screen-setup-code-hema.png").size == (500, 256)
